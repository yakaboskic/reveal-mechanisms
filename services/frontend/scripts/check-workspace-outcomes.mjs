#!/usr/bin/env node
/** Fully mocked workspace regression. No real records, jobs or publication writes. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '../../..'), origin = new URL(process.env.WORKSPACE_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname));
const output = resolve(root, '.runtime/workspace-outcomes/browser'); await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const spec = JSON.parse(await readFile(resolve(root, 'api/openapi.json'), 'utf8'));
const outcome = structuredClone(Object.values(spec.paths['/v1/analysis-outcomes/{outcome_id}'].get.responses['200'].content['application/json'].examples)[0].value);
const gap = fixture.gaps.items[0], factors = fixture.suggestions.automatic_anchors.slice(0, 2).map(item => item.factor), owner = '11111111-1111-4111-8111-111111111111';
const summary = value => Object.fromEntries(['id', 'outcome', 'summary', 'knowledge_gap', 'anchors', 'created_at', 'attribution', 'publication'].map(key => [key, value[key]]));
const paging = (items, next = null) => ({ items, page: { next_cursor: next, has_more: !!next, snapshot_id: 'mock' } });
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
const browser = await chromium.launch({ headless: true });
const report = { scope: 'All API responses mocked; no actual writes or model calls.', scenarios: [] };
async function harness(name, { mobile = false, anonymous = false, status = 'running', canClaim = false } = {}) {
  const context = await browser.newContext({ viewport: { width: mobile ? 390 : 1280, height: 950 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(12000); await page.clock.install();
  const drafts = ['active', 'editable'].map((id, index) => ({ id: `draft-${id}`, name: index ? 'Alternative mechanism' : 'Original hypothesis', version: 1, owner_user_id: owner, created_at: '2026-09-28T12:00:00Z', updated_at: '2026-09-29T12:00:00Z', composer: { source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision }, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg', 'prokn'] } }));
  const state = { drafts, conflictDraft: false, status, canClaim, owner, outcomeSummary: outcome.summary, failOutcomes: false, failSession: false, holdOutcomes: null, requests: [], errors: [], unexpected: [] };
  page.on('pageerror', error => state.errors.push(error.message));
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) { state.unexpected.push(path); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    state.requests.push({ path, cursor: url.searchParams.get('cursor'), method: req.method(), body: req.postDataJSON(), key: req.headers()['idempotency-key'] });
    const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return state.failSession ? route.fulfill({ status: 503, json: {} }) : respond({ principal: { user_id: state.owner }, canClaim: state.canClaim, providers: { google: true, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: state.owner, principal_kind: anonymous ? 'anonymous' : 'registered', display_name: anonymous ? null : state.owner === owner ? 'Workspace researcher' : 'Another researcher', workspace_expires_at: null });
    if (path === '/api/session/claim') { state.canClaim = false; return respond({ claimed: true }); }
    if (path === '/api/backend/v1/knowledge-gaps') return respond(paging([gap]));
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return respond(gap);
    if (path.startsWith(`/api/backend/v1/knowledge-gaps/${gap.object.id}/`)) return respond(paging([]));
    if (path.startsWith('/api/backend/v1/mechanisms/')) {
      const factor = factors.find(factor => path === `/api/backend/v1/mechanisms/${factor.source_id}`);
      if (factor) return respond(factor);
    }
    if (path === '/api/backend/v1/accounts') return respond(paging([]));
    if (path === '/api/backend/v1/analysis-outcomes') {
      const current = { ...outcome, summary: state.owner === owner ? state.outcomeSummary : 'Only the second user can see this exploration.' };
      if (state.holdOutcomes) { const held = state.holdOutcomes; state.holdOutcomes = null; await held; }
      if (state.failOutcomes) return route.fulfill({ status: 503, json: { code: 'UNAVAILABLE', detail: 'Workspace temporarily offline.' } });
      const later = { ...outcome, id: '77777777-7777-4777-8777-777777777777', summary: 'A second saved investigation with a different evidence gap.', publication: { ...outcome.publication, visibility: 'public' } };
      return respond(state.canClaim ? paging([]) : url.searchParams.has('cursor') ? paging([summary(later)]) : paging([summary(current)], 'outcomes-page-2'));
    }
    if (path === `/api/backend/v1/analysis-outcomes/${outcome.id}`) return respond(outcome);
    if (path === '/api/backend/v1/me/explorations') return respond(paging([{ source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision }, knowledge_gap: gap.object, last_explored_at: '2026-09-29T12:00:00Z', draft_id: 'draft-active', scientific_accounts: { count: 0 } }]));
    if (path === '/api/backend/v1/drafts') {
      if (req.method() === 'POST') {
        const body = req.postDataJSON(), draft = { ...body, id: `draft-new-${state.drafts.length}`, owner_user_id: state.owner, version: 1, created_at: '2026-09-30T12:00:00Z', updated_at: '2026-09-30T12:00:00Z' };
        state.drafts.push(draft); return respond(draft);
      }
      return respond(paging(state.drafts));
    }
    if (path.startsWith('/api/backend/v1/drafts/')) {
      const id = path.split('/').at(-1), draft = state.drafts.find(draft => draft.id === id);
      if (!draft) return route.fulfill({ status: 404, json: { detail: 'Draft unavailable.' } });
      if (req.method() === 'GET') return respond(draft);
      const body = req.postDataJSON();
      if (state.conflictDraft) { state.conflictDraft = false; draft.version++; draft.name = 'Changed in another tab'; }
      if (body.expected_version !== draft.version) return route.fulfill({ status: 409, json: { code: 'VERSION_CONFLICT', detail: 'This draft changed in another tab.' } });
      if (req.method() === 'PATCH') { Object.assign(draft, body, { version: draft.version + 1 }); return respond(draft); }
      if (req.method() === 'DELETE') { state.drafts = state.drafts.filter(d => d.id !== id); return respond({ id, deleted: true }); }
    }
    if (path === '/api/backend/v1/research-requests') return respond(url.searchParams.has('cursor') ? paging([{ id: 'request-active', source_draft_id: 'draft-active', composer: { source_gap: { id: gap.object.id } } }]) : paging([{ id: 'request-done', source_draft_id: 'draft-done', composer: { source_gap: { id: gap.object.id } } }], 'requests-page-2'));
    if (path === '/api/backend/v1/jobs') return respond(url.searchParams.has('cursor') ? paging([{ id: 'job-active', kind: 'analysis', research_request_id: 'request-active', status: state.status, created_at: '2026-09-28' }]) : paging([{ id: 'job-done', kind: 'analysis', research_request_id: 'request-done', status: 'succeeded', created_at: '2026-09-29' }], 'jobs-page-2'));
    state.unexpected.push(req.method() + ' ' + path); return route.fulfill({ status: 500, json: { detail: 'Unmocked route' } });
  });
  return { page, state, async close() {
    assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    report.scenarios.push({ name, requests: state.requests }); await context.close();
  } };
}
try {
  {
    const h = await harness('open-draft-edit-autosave-and-return', { status: 'succeeded' });
    const page = h.page;
    h.state.drafts[1].composer.eaggl_anchors = factors.map(factor => ({ reference: { source: 'eaggl', source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }));
    const openDraft = async () => {
      await page.getByRole('button', { name: 'Your workspace', exact: true }).click();
      await page.getByRole('link', { name: 'Your knowledge gaps', exact: true }).click();
      const row = page.locator('.workspace-gap-row');
      await row.locator('summary').filter({ hasText: 'Drafts (2)' }).click();
      await row.getByRole('combobox', { name: 'Drafts', exact: true }).selectOption('draft-editable');
      await row.getByRole('link', { name: 'Open draft', exact: true }).click();
      await page.waitForFunction(() => JSON.parse(sessionStorage.getItem('reveal:composer') || 'null')?.draft?.id === 'draft-editable');
      assert.equal(await page.locator('.selected-question').textContent(), gap.object.text);
      await page.getByRole('navigation', { name: 'Draft navigation' }).getByText('Alternative mechanism', { exact: true }).waitFor();
    };
    await page.goto(origin + '/?draft=draft-active');
    await page.locator('.selected-question').waitFor();
    await openDraft();
    assert.equal(await page.locator('.anchor-chips .chip').count(), 2);
    await page.screenshot({ path: resolve(output, 'opened-draft.png'), fullPage: true });
    await page.locator('.anchor-chips .remove').first().click();
    const saved = page.waitForResponse(response => response.request().method() === 'PATCH' && response.url().endsWith('/drafts/draft-editable'));
    await page.clock.fastForward(1100); await saved;
    assert.equal(h.state.drafts[1].composer.eaggl_anchors.length, 1);
    assert.equal(h.state.drafts[0].version, 1, 'Editing the selected draft must not alter another draft');
    await page.reload();
    await page.locator('.selected-question').waitFor();
    assert.equal(await page.locator('.anchor-chips .chip').count(), 1);
    await openDraft();
    assert.equal(await page.locator('.anchor-chips .chip').count(), 1);
    const pending = { method: 'session', question: 'An unfinished earlier submission', gap, composer: h.state.drafts[1].composer, draft: h.state.drafts[1], owner, anonymousKey: 'pending-attempt', requestKeys: [], submitKey: null };
    await page.evaluate(pending => sessionStorage.setItem('reveal:submission', JSON.stringify(pending)), pending);
    await page.reload();
    await page.locator('.selected-question').waitFor();
    assert.equal(await page.locator('.anchor-chips .chip').count(), 1);
    assert.equal(await page.locator('.submission-page').count(), 0, 'An explicit draft link opens the editor even with pending submission state');
    assert.equal(h.state.requests.filter(request => request.method === 'POST' && request.path.endsWith('/jobs')).length, 0, 'Opening a draft must never restart a saved submission');
    await h.close();
  }
  for (const mobile of [false, true]) {
    const h = await harness(mobile ? 'anonymous-mobile-explorations' : 'registered-explorations', { mobile, anonymous: mobile });
    await h.page.goto(origin + '/workspace?tab=explorations');
    await h.page.getByText(outcome.summary, { exact: true }).waitFor();
    await h.page.getByRole('tab', { name: 'Explorations 1+' }).waitFor();
    assert.equal(await h.page.locator('.workspace-continuity').count(), mobile ? 1 : 0, 'Only anonymous visitors see the workspace sign-in card');
    assert.equal(h.state.requests.filter(r => r.path.includes('/analysis-outcomes/')).length, 0, 'Only summary list fetched');
    await h.page.getByRole('button', { name: 'Load more', exact: true }).click();
    await h.page.getByText('A second saved investigation with a different evidence gap.', { exact: true }).waitFor();
    await h.page.getByRole('tab', { name: 'Explorations 2' }).waitFor();
    assert.equal(await h.page.getByRole('button', { name: 'Load more', exact: true }).count(), 0);
    await h.page.screenshot({ path: resolve(output, `${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
    await h.page.getByRole('tab', { name: 'Scientific accounts' }).click();
    await h.page.getByText(/Runs without a supported account are saved under Explorations/).waitFor();
    await h.page.getByRole('tab', { name: 'Scientific accounts 0' }).focus(); await h.page.keyboard.press('ArrowRight');
    await h.page.getByText(outcome.summary, { exact: true }).waitFor();
    assert.equal(await h.page.getByRole('tab', { name: /Explorations/ }).getAttribute('aria-selected'), 'true');
    await h.page.getByRole('link', { name: 'View findings and evidence gaps', exact: true }).first().click();
    await h.page.locator('.outcome-page').waitFor(); assert.equal(new URL(h.page.url()).pathname, `/analyses/${outcome.id}`);
    await h.close();
  }
  const cached = await harness('cached-tabs-navigation-revalidation-and-owner-isolation');
  const { page, state } = cached;
  const outcomeReads = () => state.requests.filter(r => r.path === '/api/backend/v1/analysis-outcomes').length;
  await page.goto(origin + '/workspace?tab=explorations');
  await page.getByText(outcome.summary, { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Load more', exact: true }).click();
  await page.getByRole('tab', { name: 'Explorations 2', exact: true }).waitFor();
  const initialReads = outcomeReads();
  await page.getByRole('tab', { name: 'Scientific accounts', exact: true }).click();
  await page.getByRole('tab', { name: 'Scientific accounts 0', exact: true }).waitFor();
  await page.getByRole('tab', { name: 'Knowledge gaps', exact: true }).click();
  await page.locator('.workspace-gap-row').waitFor();
  await page.getByRole('tab', { name: 'Explorations 2', exact: true }).click();
  await page.getByText(outcome.summary, { exact: true }).waitFor();
  assert.equal(outcomeReads(), initialReads, 'Fresh tab changes must not refetch');
  await page.getByRole('link', { name: 'View findings and evidence gaps', exact: true }).first().click();
  await page.locator('.outcome-page').waitFor();
  await page.getByRole('button', { name: 'Your workspace', exact: true }).click();
  await page.getByRole('link', { name: 'Your explorations', exact: true }).click();
  await page.getByRole('tab', { name: 'Explorations 2', exact: true }).waitFor();
  assert.equal(outcomeReads(), initialReads, 'Returning from another route retains all loaded pages');
  let release;
  state.holdOutcomes = new Promise(resolve => { release = resolve; });
  await page.clock.fastForward(31_000);
  await page.locator('#workspace-results[aria-busy="true"]').waitFor();
  assert.equal(await page.getByText('Refreshing your workspace…', { exact: true }).count(), 0);
  assert.ok(await page.getByText(outcome.summary, { exact: true }).isVisible());
  assert.equal(await page.getByText('Loading your explorations', { exact: true }).count(), 0);
  state.failOutcomes = true; release();
  await page.getByRole('alert').filter({ hasText: 'showing previously loaded records' }).waitFor();
  assert.ok(await page.getByText(outcome.summary, { exact: true }).isVisible());
  state.failSession = true;
  const offlineCheck = page.waitForResponse(response => new URL(response.url()).pathname === '/api/session/status' && response.status() === 503);
  await page.evaluate(() => window.dispatchEvent(new Event('focus'))); await offlineCheck;
  await page.getByRole('alert').filter({ hasText: 'showing previously loaded records' }).waitFor();
  assert.ok(await page.getByText(outcome.summary, { exact: true }).isVisible(), 'A transient identity-check outage must not discard cached rows');
  state.failSession = false;
  state.failOutcomes = false; state.outcomeSummary = 'Updated exploration from background revalidation.';
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await page.getByText(state.outcomeSummary, { exact: true }).waitFor();
  await page.getByRole('tab', { name: 'Explorations 2', exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, 'cache-refreshed.png'), fullPage: true });
  state.holdOutcomes = new Promise(resolve => { release = resolve; });
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.locator('#workspace-results[aria-busy="true"]').waitFor();
  assert.equal(await page.getByText('Refreshing your workspace…', { exact: true }).count(), 0);
  state.owner = '22222222-2222-4222-8222-222222222222';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.getByText('Only the second user can see this exploration.', { exact: true }).waitFor();
  release();
  assert.equal(await page.getByText(state.outcomeSummary, { exact: true }).count(), 0, 'A late response for the previous identity must not restore private rows');
  assert.equal(await page.getByRole('tab', { name: 'Explorations 1+', exact: true }).count(), 1);
  await cached.close();
  for (const status of ['queued', 'running', 'cancel_requested']) {
    const h = await harness('active-' + status, { status }); await h.page.goto(origin + '/workspace?tab=gaps');
    const row = h.page.locator('.workspace-gap-row'); await row.waitFor();
    await row.locator('summary').filter({ hasText: 'Drafts (2)' }).click();
    const resume = row.getByRole('link', { name: 'Open draft', exact: true }); assert.equal(await resume.count(), 0);
    assert.equal(await row.locator('.workspace-item-title').getAttribute('href'), '/?job=job-active&draft=draft-active');
    assert.equal(await row.getByRole('link', { name: 'Research ' + status.replaceAll('_', ' '), exact: true }).count(), 1);
    assert.ok(h.state.requests.some(r => r.cursor === 'jobs-page-2')); assert.ok(h.state.requests.some(r => r.cursor === 'requests-page-2'));
    if (status === 'running') {
      await h.page.screenshot({ path: resolve(output, 'running.png'), fullPage: true });
      const frozenReads = h.state.requests.filter(r => r.path.endsWith('/research-requests')).length;
      await h.page.clock.fastForward(11000);
      await h.page.getByRole('button', { name: 'Refresh', exact: true }).waitFor({ state: 'visible' });
      assert.equal(h.state.requests.filter(r => r.path.endsWith('/research-requests')).length, frozenReads, 'Known frozen requests are reused during active-job polling');
      h.state.status = 'failed'; await h.page.clock.fastForward(11000);
      await row.locator('a[href="/?draft=draft-active"]').filter({ hasText: 'Open draft' }).waitFor();
      assert.equal(await resume.count(), 1);
    }
    await h.close();
  }
  for (const mobile of [false, true]) {
    const h = await harness(mobile ? 'draft-crud-mobile' : 'draft-crud-desktop', { mobile });
    const page = h.page; await page.goto(origin + '/workspace?tab=gaps');
    const row = page.locator('.workspace-gap-row'), selector = row.getByRole('combobox', { name: /Drafts/ });
    await row.locator('summary').filter({ hasText: 'Drafts (2)' }).waitFor();
    await page.screenshot({ path: resolve(output, `drafts-collapsed-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
    await row.locator('summary').filter({ hasText: 'Drafts (2)' }).click();
    await selector.waitFor();
    assert.equal(await selector.inputValue(), 'draft-active');
    assert.ok(await row.getByRole('button', { name: 'Delete', exact: true }).isDisabled());
    assert.equal(await row.getByRole('link', { name: 'Open draft', exact: true }).count(), 0);
    await selector.selectOption('draft-editable');
    assert.equal(await row.getByRole('link', { name: 'Open draft', exact: true }).getAttribute('href'), '/?draft=draft-editable');
    assert.ok(await row.getByRole('button', { name: 'Delete', exact: true }).isEnabled());
    await row.getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('BMPR2 modifier hypothesis');
    await page.getByRole('button', { name: 'Save name', exact: true }).click();
    await selector.getByRole('option', { name: /BMPR2 modifier hypothesis/ }).waitFor({ state: 'attached' });
    assert.equal(h.state.drafts.find(d => d.id === 'draft-editable').version, 2);
    await row.getByRole('button', { name: '+ New draft', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Follow-up mechanism');
    assert.ok(await page.getByRole('checkbox', { name: /Copy anchors and settings/ }).isChecked());
    await page.getByRole('button', { name: 'Create draft', exact: true }).click();
    await selector.getByRole('option', { name: /Follow-up mechanism/ }).waitFor({ state: 'attached' });
    assert.equal(await selector.inputValue(), 'draft-new-2');
    assert.deepEqual(h.state.drafts[2].composer, h.state.drafts[1].composer);
    await page.screenshot({ path: resolve(output, `drafts-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
    h.state.conflictDraft = true;
    await row.getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Stale rename');
    await page.getByRole('button', { name: 'Save name', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'This draft changed in another tab.' }).waitFor();
    await selector.getByRole('option', { name: /Changed in another tab/ }).waitFor({ state: 'attached' });
    assert.equal(h.state.drafts[2].name, 'Changed in another tab');
    await row.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByText(/research runs, scientific accounts, and saved explorations will remain available/).waitFor();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(h.state.drafts.length, 3);
    await row.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('button', { name: 'Delete draft', exact: true }).click();
    await selector.getByRole('option', { name: /Changed in another tab/ }).waitFor({ state: 'detached' });
    assert.equal(h.state.drafts.length, 2);
    assert.equal(await selector.inputValue(), 'draft-active');
    // Delete the last drafts after research completes; the knowledge gap and run remain.
    h.state.status = 'succeeded'; await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await row.getByRole('link', { name: 'Open draft', exact: true }).waitFor();
    for (const id of ['draft-editable', 'draft-active']) {
      await selector.selectOption(id); await row.getByRole('button', { name: 'Delete', exact: true }).click();
      await page.getByRole('button', { name: 'Delete draft', exact: true }).click();
      await selector.getByRole('option', { name: id === 'draft-editable' ? /BMPR2 modifier hypothesis/ : /Original hypothesis/ }).waitFor({ state: 'detached' });
    }
    await row.getByText('No drafts yet', { exact: true }).waitFor();
    assert.ok((await row.getByRole('link', { name: 'Research succeeded', exact: true }).getAttribute('href')).includes('&gap='));
    await row.getByRole('button', { name: '+ New draft', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Fresh start');
    assert.equal(await page.getByRole('checkbox', { name: /Copy anchors/ }).count(), 0);
    await page.getByRole('button', { name: 'Create draft', exact: true }).click();
    await selector.getByRole('option', { name: /Fresh start/ }).waitFor({ state: 'attached' });
    assert.equal(h.state.drafts[0].composer.source_gap.id, gap.object.id);
    assert.equal(h.state.drafts[0].composer.eaggl_anchors.length, 0);
    assert.ok(h.state.requests.filter(r => ['POST', 'PATCH', 'DELETE'].includes(r.method) && r.path.includes('/drafts')).every(r => r.key));
    await h.close();
  }
  const claim = await harness('same-user-claim-refresh', { canClaim: true });
  await claim.page.goto(origin + '/workspace?tab=explorations');
  await claim.page.getByText('Room for your findings.', { exact: true }).waitFor();
  await claim.page.getByRole('button', { name: 'Move my anonymous work', exact: true }).click();
  await claim.page.getByText(outcome.summary, { exact: true }).waitFor(); await claim.close();
  report.status = 'passed'; console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
} catch (error) { report.status = 'failed'; report.error = String(error); throw error; }
finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2)); await browser.close(); }
