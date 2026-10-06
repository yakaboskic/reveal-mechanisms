#!/usr/bin/env node
/** Fully intercepted workspace regression: no real records, authentication, jobs, or model calls.
 * Editor save/discard behavior is covered separately; this checks independent workspace collections.
 */
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
const gap = fixture.gaps.items[0], owner = '11111111-1111-4111-8111-111111111111';
const selectedGap = { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision };
const summary = value => Object.fromEntries(['id', 'outcome', 'summary', 'knowledge_gap', 'anchors', 'created_at', 'attribution', 'publication'].map(key => [key, value[key]]));
const paging = (items, next = null) => ({ items, page: { next_cursor: next, has_more: !!next, snapshot_id: 'mock' } });
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
const browser = await chromium.launch({ headless: true });
const report = { scope: 'All API responses mocked; no actual writes or model calls.', scenarios: [] };
async function harness(name, { mobile = false, anonymous = false } = {}) {
  const context = await browser.newContext({ viewport: { width: mobile ? 390 : 1280, height: 950 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const drafts = ['active', 'editable', 'temporary'].map((id, index) => ({ id: `draft-${id}`, lifecycle: index === 2 ? 'temporary' : 'saved', name: ['Original hypothesis', 'Alternative mechanism', null][index], version: 1, owner_user_id: owner,
    created_at: '2026-09-28T12:00:00Z', updated_at: '2026-09-29T12:00:00Z', composer: { source_gap: selectedGap,
      research_direction: 'Saved direction ' + id, context: 'Saved researcher context', hypotheses: 'A saved working hypothesis', upload_ids: ['document-reference'],
      eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg', 'prokn'] } }));
  const state = { drafts, conflictDraft: false, status: 'running', owner, outcomeSummary: outcome.summary, failOutcomes: false, calls: [], errors: [], unexpected: [], cursor: 0, collections: [] };
  state.localWorks = [{ id: 'local-ready', state: 'preparing', created_at: '2026-09-29T12:00:00Z', grants: [], submissions: [],
    request: { question_id: gap.object.id, document: { knowledge_gaps: [gap.object] }, composer: { research_direction: 'Investigate with my local agent' } } }];
  page.on('pageerror', error => state.errors.push(error.message));
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) { state.unexpected.push(path); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    state.calls.push({ path, cursor: url.searchParams.get('cursor'), method: req.method(), key: req.headers()['idempotency-key'] });
    const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return respond({ principal: { user_id: state.owner }, canClaim: false, providers: { google: false, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: state.owner, principal_kind: anonymous ? 'anonymous' : 'registered', display_name: anonymous ? null : 'Workspace researcher', workspace_expires_at: null });
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' + (state.cursor ? `event: workspace_change\ndata: ${JSON.stringify({ schema_version: 1, scope: 'workspace', cursor: String(state.cursor), collections: state.collections })}\n\n` : '') });
    if (path === '/api/backend/v1/accounts') return respond(paging([]));
    if (path === '/api/backend/v1/local-work') return respond({ items: state.localWorks });
    if (path === '/api/backend/v1/analysis-outcomes') {
      if (state.failOutcomes) return route.fulfill({ status: 503, json: { code: 'UNAVAILABLE', detail: 'Workspace temporarily offline.' } });
      const later = { ...outcome, id: '77777777-7777-4777-8777-777777777777', summary: 'A second saved investigation.', publication: { ...outcome.publication, visibility: 'public' } };
      return respond(url.searchParams.has('cursor') ? paging([summary(later)]) : paging([summary({ ...outcome, summary: state.outcomeSummary })], 'outcomes-page-2'));
    }
    if (path === '/api/backend/v1/me/explorations') return respond(paging([{ source_gap: selectedGap, knowledge_gap: gap.object, last_explored_at: '2026-09-29T12:00:00Z', draft_id: 'draft-active', scientific_accounts: { count: 0 } }]));
    if (path === '/api/backend/v1/drafts') {
      if (req.method() === 'POST') {
        const body = req.postDataJSON(), draft = { ...body, lifecycle: body.lifecycle || 'saved', id: `draft-new-${state.drafts.length}`, owner_user_id: owner, version: 1, created_at: '2026-09-30T12:00:00Z', updated_at: '2026-09-30T12:00:00Z' };
        state.drafts.push(draft); return respond(draft);
      }
      // Deliberately includes temporary data to verify the UI's defensive filter too.
      return respond(paging(state.drafts));
    }
    if (path.startsWith('/api/backend/v1/drafts/')) {
      const id = path.split('/').at(-1), draft = state.drafts.find(draft => draft.id === id);
      if (!draft) return route.fulfill({ status: 404, json: { detail: 'Draft unavailable.' } });
      const body = req.postDataJSON();
      if (state.conflictDraft) { state.conflictDraft = false; draft.version++; draft.name = 'Changed in another tab'; }
      if (body.expected_version !== draft.version) return route.fulfill({ status: 409, json: { code: 'VERSION_CONFLICT', detail: 'This draft changed in another tab.' } });
      if (req.method() === 'PATCH') { Object.assign(draft, body, { version: draft.version + 1 }); return respond(draft); }
      if (req.method() === 'DELETE') { state.drafts = state.drafts.filter(d => d.id !== id); return respond({ id, deleted: true }); }
    }
    if (path === '/api/backend/v1/research-requests') return respond(paging(['active', 'done'].map(id => ({ id: `request-${id}`, source_draft_id: id === 'active' ? 'draft-active' : 'deleted-draft', source_draft_version: 1,
      question_id: gap.object.id, document: { knowledge_gaps: [gap.object] }, composer: { source_gap: selectedGap, research_direction: `Frozen direction ${id}` } }))));
    if (path === '/api/backend/v1/jobs') return respond(url.searchParams.has('cursor') ? paging([{ id: 'job-active', kind: 'analysis', research_request_id: 'request-active', status: state.status, created_at: '2026-09-28' }]) : paging([
      { id: 'job-done', kind: 'analysis', research_request_id: 'request-done', status: 'insufficient_evidence', created_at: '2026-09-29', result: { kind: 'analysis_outcome', outcome_id: outcome.id } },
      { id: 'paragraph-job', kind: 'paragraph', status: 'succeeded', created_at: '2026-09-30' }], 'jobs-page-2'));
    state.unexpected.push(req.method() + ' ' + path); return route.fulfill({ status: 500, json: { detail: 'Unmocked route' } });
  });
  return { page, state, async change(collections, path) {
    const response = page.waitForResponse(response => new URL(response.url()).pathname === path);
    state.collections = collections; state.cursor++;
    await response; await page.locator('#workspace-results[aria-busy="false"]').waitFor();
  }, async close() {
    assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: resolve(output, name + '.png'), fullPage: true });
    report.scenarios.push({ name, calls: state.calls }); await context.close();
  } };
}
try {
  for (const mobile of [false, true]) {
    const h = await harness(`independent-collections-${mobile ? 'mobile' : 'desktop'}`, { mobile });
    const { page, state } = h;
    await page.goto(origin + '/workspace?tab=drafts');
    const rows = page.locator('.workspace-draft-row');
    await rows.nth(1).waitFor(); assert.equal(await rows.count(), 2);
    assert.equal(state.calls.filter(call => /\/(jobs|research-requests)$/.test(call.path)).length, 0);
    const original = page.locator('[data-draft-id="draft-active"]');
    assert.equal(await original.getByRole('link', { name: 'Open draft', exact: true }).getAttribute('href'), '/drafts/draft-active');
    assert.equal(await original.getByRole('button', { name: 'Delete', exact: true }).isEnabled(), true);
    await page.getByRole('tab', { name: 'Research runs', exact: true }).click();
    await page.locator('[data-job-id="job-active"]').waitFor();
    assert.equal(await page.locator('.workspace-run-row').count(), 3);
    const local = page.locator('[data-local-work-id="local-ready"]');
    assert.equal(await page.locator('.workspace-run-row').first().getAttribute('data-local-work-id'), 'local-ready');
    await page.getByRole('tab', { name: 'Research runs 3', exact: true }).waitFor();
    await local.getByText('Local', { exact: true }).waitFor();
    await local.getByText('Preparing research seed', { exact: true }).waitFor();
    assert.equal(await local.getByRole('link', { name: 'View run', exact: false }).getAttribute('href'), '/local-runs/local-ready');
    assert.equal(await page.locator('.workspace-topbar a[href="/local-runs"]').count(), 0);
    state.localWorks[0].state = 'ready';
    state.localWorks[0].submissions = [{ id: 'validation', state: 'succeeded', validation_only: true, account_ids: ['candidate-account'], reused_account_ids: ['candidate-reuse'] }, { id: 'submission', state: 'accepted', account_ids: ['new-local-account'], reused_account_ids: ['existing-account'] }, { id: 'another', state: 'accepted', reused_account_ids: ['existing-account'] }];
    await h.change(['jobs'], '/api/backend/v1/local-work');
    await local.getByText('Ready for your local agent', { exact: true }).waitFor();
    assert.equal(await local.getByRole('link', { name: 'Scientific account', exact: true }).count(), 2);
    assert.equal(await local.locator('a[href="/accounts/existing-account"]').count(), 1);
    assert.equal(await local.locator('a[href*="candidate"]').count(), 0, 'Validation candidates are not accepted findings');
    await page.locator('[data-job-id="job-active"]').getByText('Online', { exact: true }).waitFor();
    await page.screenshot({ path: resolve(output, `unified-runs-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
    assert.equal(await page.locator('[data-job-id="job-active"]').getByRole('link', { name: 'View run', exact: false }).getAttribute('href'), '/runs/job-active');
    await page.getByText('Frozen direction active', { exact: true }).waitFor();
    assert.equal(await page.getByText('Saved direction active', { exact: true }).count(), 0);
    assert.equal(await page.getByRole('link', { name: 'View exploration', exact: true }).getAttribute('href'), `/analyses/${outcome.id}`);
    for (const status of ['succeeded', 'failed', 'cancelled', 'insufficient_evidence']) {
      state.status = status; await h.change(['jobs'], '/api/backend/v1/jobs');
      await page.getByRole('tab', { name: /Saved drafts/ }).click();
      await original.waitFor(); assert.equal(await rows.count(), 2);
      await page.getByRole('tab', { name: /Research runs/ }).click();
    }
    await page.getByRole('tab', { name: /Saved drafts/ }).click();
    const editable = page.locator('[data-draft-id="draft-editable"]');
    await editable.getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Renamed saved direction');
    await page.getByRole('button', { name: 'Save name', exact: true }).click();
    await editable.getByRole('link', { name: 'Renamed saved direction', exact: true }).waitFor();
    await editable.getByRole('button', { name: 'Copy', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Copy with documents');
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    const copy = page.locator('[data-draft-id="draft-new-3"]'); await copy.waitFor();
    assert.deepEqual(state.drafts[3].composer, state.drafts[1].composer);
    assert.equal(state.drafts[3].lifecycle, 'saved');
    state.conflictDraft = true;
    await copy.getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Stale name');
    await page.getByRole('button', { name: 'Save name', exact: true }).click();
    await copy.getByRole('alert').filter({ hasText: 'changed in another tab' }).waitFor();
    await copy.getByRole('link', { name: 'Changed in another tab', exact: true }).waitFor();
    // Delete the source of a run while it is active. The run remains independently accessible.
    state.status = 'running';
    await original.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByText(/frozen inputs will remain available/).waitFor();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click(); assert.equal(await original.count(), 1);
    await original.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('button', { name: 'Delete draft', exact: true }).click(); await original.waitFor({ state: 'detached' });
    await page.getByRole('tab', { name: /Research runs/ }).click();
    await h.change(['jobs'], '/api/backend/v1/jobs');
    await page.locator('[data-job-id="job-active"]').getByText('running', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Knowledge gaps', exact: true }).click();
    const gapRow = page.locator('.workspace-gap-row'); await gapRow.waitFor();
    assert.equal(await gapRow.locator('.workspace-item-title').getAttribute('href'), `/knowledge-gaps/${encodeURIComponent(gap.object.id)}`);
    assert.equal(await gapRow.locator('a[href="/runs/job-active"]').count(), 1);
    assert.equal(await gapRow.locator('a[href="/drafts/draft-editable"]').count(), 1);
    assert.equal(await gapRow.getByRole('combobox').count(), 0);
    await gapRow.getByRole('button', { name: '+ New saved draft', exact: true }).click();
    await page.getByRole('textbox', { name: 'Draft name', exact: true }).fill('Fresh start');
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await gapRow.getByRole('link', { name: 'Fresh start', exact: true }).waitFor();
    const fresh = state.drafts.find(draft => draft.name === 'Fresh start'); assert.equal(fresh.composer.source_gap.id, gap.object.id); assert.deepEqual(fresh.composer.upload_ids, []);
    assert.ok(state.calls.filter(call => ['POST', 'PATCH', 'DELETE'].includes(call.method)).every(call => call.key));
    await page.goto(origin + '/local-runs');
    await page.waitForURL('**/workspace?tab=runs');
    await page.locator('[data-local-work-id="local-ready"]').waitFor();
    await h.close();
  }
  for (const mobile of [false, true]) {
    const h = await harness(`exploration-cache-${mobile ? 'mobile' : 'desktop'}`, { mobile, anonymous: mobile });
    const { page, state } = h;
    await page.goto(origin + '/workspace?tab=explorations');
    await page.getByText(outcome.summary, { exact: true }).waitFor();
    assert.equal(await page.locator('.workspace-continuity').count(), mobile ? 1 : 0);
    await page.getByRole('button', { name: 'Load more', exact: true }).click();
    await page.getByText('A second saved investigation.', { exact: true }).waitFor();
    const reads = () => state.calls.filter(call => call.path.endsWith('/analysis-outcomes')).length;
    const loadedReads = reads();
    await page.getByRole('tab', { name: 'Scientific accounts', exact: true }).click();
    await page.getByText(/Runs without a supported account are saved under Explorations/).waitFor();
    await page.getByRole('tab', { name: /Explorations/ }).click();
    await page.getByText(outcome.summary, { exact: true }).waitFor(); assert.equal(reads(), loadedReads);
    state.failOutcomes = true; await h.change(['explorations'], '/api/backend/v1/analysis-outcomes');
    await page.getByRole('alert').filter({ hasText: 'showing previously loaded records' }).waitFor();
    assert.equal(await page.getByText(outcome.summary, { exact: true }).isVisible(), true);
    state.failOutcomes = false; state.outcomeSummary = 'Refreshed exploration summary';
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await page.getByText(state.outcomeSummary, { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Explorations 2', exact: true }).waitFor();
    await h.close();
  }
  report.status = 'passed'; console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
} catch (error) { report.status = 'failed'; report.error = String(error); throw error; }
finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2)); await browser.close(); }
