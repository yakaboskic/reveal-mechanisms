#!/usr/bin/env node
/** All API requests are mocked. No real publication, authentication, or research execution. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '../../..');
const output = resolve(root, '.runtime/analysis-outcome-audit'); await mkdir(output, { recursive: true });
const origin = process.env.OUTCOME_BASE_URL || 'http://127.0.0.1:3000';
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname));
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || resolve(homedir(), 'Library/Caches/ms-playwright/chromium-1148/chrome-mac/Chromium.app/Contents/MacOS/Chromium') });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const owner = '11111111-1111-4111-8111-111111111111', outcomeId = '22222222-2222-4222-8222-222222222222', jobId = '33333333-3333-4333-8333-333333333333';
const gap = structuredClone(fixture.gaps.items[0]); gap.object.text = 'Do the selected mouse models reproduce the human phenotype?';
const paging = { has_more: false, next_cursor: null, snapshot_id: 'fixture' };
const publication = (visibility, version, can_manage) => ({ visibility, version, can_manage, published_at: visibility === 'public' ? '2026-09-28T10:00:00Z' : null, updated_at: '2026-09-28T10:00:00Z', has_unpublished_changes: false });
function record(canManage, visibility = 'public') { return { id: outcomeId, outcome: 'insufficient_evidence', summary: 'The retained evidence does not resolve the species difference. A directly relevant experimental comparison is still needed.', reason: 'The observed sources lack a relevant CFDE-backed link to the selected question.', explored_topics: ['Compared the retained mechanism loadings and disease reports.'], missing_evidence: ['A relevant genetic evidence link and direct model comparison.'], limitations: ['Only the sources retained in this attempt were assessed.'], next_steps: ['Revisit this question when relevant experimental evidence is available.'], knowledge_gap: gap.object, source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision }, anchors: [{ source_id: 'factor:portal:FG:cfde-inc-v2:Factor3', mechanism_id: 'dapper:Mechanism.fixture', name: 'Glucose regulation', trait: 'FG', origin: 'automatic' }], selected_kgs: ['prokn'], created_at: '2026-09-28T10:00:00Z', attribution: { user_id: owner, principal_kind: 'anonymous', display_name: null }, scope_note: 'This is a record of an exploration, not a validated scientific account or evidence that a biological relationship is absent.', record_format: 'structured', provenance: { evidence_package_sha256: 'a'.repeat(64), outcome_sha256: 'b'.repeat(64), runtime_sha256: 'c'.repeat(64), ledger_sha256: 'd'.repeat(64), source_bindings: [], source_artifacts: [], coverage: {}, evidence_refs: [], graph_queries: [] }, job_id: canManage ? jobId : null, publication: publication(visibility, 0, canManage) }; }
const report = { scope: 'Invented mocked UI fixtures; no actual writes, models, authentication or publication.', scenarios: [] };
async function until(check, message) { const started = performance.now(); while (!await check()) { assert.ok(performance.now() - started < 12000, message); await new Promise(resolve => setTimeout(resolve, 20)); } }
async function harness(name, { owned = false, mobile = false, failOnce = false, failRefreshOnWrite = false, canClaim = false } = {}) {
  const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 1000 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(12000);
  const state = { outcome: record(owned, owned ? 'private' : 'public'), principal: owned ? owner : null, canClaim,
    requests: [], writes: [], errors: [], unexpected: [], failOnce, failRefreshOnWrite, failDetailReads: 0, holdDetail: false, heldDetail: [] };
  page.on('pageerror', error => state.errors.push(error.message));
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname), method = request.method();
    if (url.origin !== origin) { state.unexpected.push(url.origin + path); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    state.requests.push({ method, path }); const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return respond({ principal: state.principal ? { user_id: state.principal } : null, canClaim: state.canClaim, providers: { google: false, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: state.principal, principal_kind: 'anonymous', display_name: null, workspace_expires_at: '2026-10-28T00:00:00Z' });
    if (path === '/api/session/claim') {
      state.principal = '55555555-5555-4555-8555-555555555555'; state.canClaim = false;
      state.outcome = record(false); state.outcome.summary = 'Only the public snapshot is available after changing identity.';
      state.holdDetail = true; return respond({ claimed: true });
    }
    if (path === `/api/backend/v1/analysis-outcomes/${outcomeId}` || path === `/api/backend/v1/jobs/${jobId}/outcome`) {
      if (state.holdDetail) await new Promise(resolve => state.heldDetail.push(resolve));
      if (state.failDetailReads > 0) { state.failDetailReads--; return route.fulfill({ status: 503, json: { detail: 'Mocked detail refresh unavailable.' } }); }
      return respond(state.outcome);
    }
    if (path === `/api/backend/v1/analysis-outcomes/${outcomeId}/publication`) {
      if (method === 'GET') return respond(state.outcome.publication);
      const body = request.postDataJSON(); state.writes.push({ body, key: request.headers()['idempotency-key'] });
      if (state.failOnce) { state.failOnce = false; return route.fulfill({ status: 504, json: { code: 'UNCERTAIN_RESPONSE', detail: 'Fixture response was interrupted.' } }); }
      state.outcome.publication = publication(body.visibility, body.expected_version + 1, true);
      if (state.failRefreshOnWrite) { state.failRefreshOnWrite = false; state.failDetailReads = 1; }
      return respond(state.outcome.publication);
    }
    if (path === `/api/backend/v1/jobs/${jobId}`) return respond({ id: jobId, owner_user_id: owner, research_request_id: '44444444-4444-4444-8444-444444444444', input_account_id: null, created_at: '2026-09-28T09:55:00Z', updated_at: '2026-09-28T10:00:00Z', links: {}, kind: 'analysis', status: 'insufficient_evidence', stage: 'authoring_account', result: { kind: 'analysis_outcome', outcome_id: outcomeId, evidence_package_sha256: 'a'.repeat(64) }, failure: null, warnings: [], last_event_id: '0', completed_at: '2026-09-28T10:00:00Z' });
    if (path === `/api/backend/v1/jobs/${jobId}/events`) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': complete\n\n' });
    if (path === '/api/backend/v1/knowledge-gaps') return respond({ items: [gap], page: paging });
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return respond(gap);
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}/accounts`) return respond({ items: [], page: paging });
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}/outcomes`) return respond({ items: [state.outcome], page: paging });
    if (path === '/api/backend/v1/mechanisms/suggest') return respond({ ...fixture.suggestions, automatic_anchors: [], limitations: [] });
    if (path === '/api/backend/v1/me/explorations') return respond({});
    if (/^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', headers: { 'Content-Disposition': 'attachment; filename="source.json"' }, body: '{"fixture":true}' });
    state.unexpected.push(`${method} ${path}`); return route.fulfill({ status: 500, json: { code: 'UNMOCKED', detail: 'Blocked unmocked API.' } });
  });
  return { page, state, async close() { assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []); report.scenarios.push({ name, requests: state.requests, publicationWrites: state.writes }); await context.close(); } };
}
try {
  const reader = await harness('public-mobile', { mobile: true });
  await reader.page.goto(`${origin}/analyses/${outcomeId}`);
  await reader.page.getByRole('heading', { name: gap.object.text }).waitFor();
  assert.equal(await reader.page.getByText('Published exploration', { exact: true }).count(), 1);
  assert.equal(await reader.page.locator('.outcome-publication').count(), 0);
  assert.equal(await reader.page.getByRole('link', { name: 'View private activity' }).count(), 0);
  assert.equal(await reader.page.locator('.outcome-anchor small').innerText(), 'FG');
  assert.ok(await reader.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await reader.page.screenshot({ path: resolve(output, 'public-mobile.png'), fullPage: true });
  assert.ok(!reader.state.requests.some(request => request.path.includes('/jobs/'))); await reader.close();

  const publisher = await harness('owner-publication-retry', { owned: true, failOnce: true });
  await publisher.page.goto(`${origin}/analyses/${outcomeId}`);
  const panel = publisher.page.locator('.outcome-publication');
  await panel.getByRole('button', { name: 'Publish exploration…', exact: true }).click();
  await panel.getByText(/complete private evidence package stay private/).waitFor(); assert.equal(publisher.state.writes.length, 0);
  await panel.getByRole('button', { name: 'Publish exploration', exact: true }).click();
  await panel.getByRole('button', { name: 'Retry', exact: true }).click();
  await panel.getByRole('button', { name: 'Unpublish', exact: true }).waitFor();
  assert.deepEqual(publisher.state.writes[0], publisher.state.writes[1]);
  await panel.getByRole('button', { name: 'Unpublish', exact: true }).click();
  await panel.getByRole('button', { name: 'Publish exploration…', exact: true }).waitFor();
  await publisher.page.screenshot({ path: resolve(output, 'owner-desktop.png'), fullPage: true }); await publisher.close();

  const job = await harness('completed-job-outcome-link', { owned: true });
  await job.page.goto(`${origin}/?job=${jobId}`); await job.page.locator('.outcome-job .outcome-status').waitFor();
  assert.match(await job.page.locator('.outcome-job .outcome-status').innerText(), /Exploration saved/);
  assert.equal(await job.page.locator('.outcome-job a').getAttribute('href'), `/analyses/${outcomeId}`);
  await job.close();

  const listing = await harness('public-gap-discovery');
  await listing.page.goto(`${origin}/?gap=${encodeURIComponent(gap.object.id)}`);
  await listing.page.locator('.gap-outcomes .gap-account-card').waitFor();
  assert.equal(await listing.page.locator('.gap-accounts:not(.gap-outcomes) .gap-account-card').count(), 0);
  await listing.page.locator('.gap-outcomes').getByRole('link', { name: 'View findings and evidence gaps' }).click();
  await listing.page.getByRole('heading', { name: 'Evidence still needed' }).waitFor(); await listing.close();

  const repeat = await harness('uncertain-primary-button-keeps-key', { owned: true, failOnce: true });
  await repeat.page.goto(`${origin}/analyses/${outcomeId}`);
  const repeatedPanel = repeat.page.locator('.outcome-publication');
  await repeatedPanel.getByRole('button', { name: 'Publish exploration…', exact: true }).click();
  await repeatedPanel.getByRole('button', { name: 'Publish exploration', exact: true }).click();
  await repeatedPanel.getByRole('alert').waitFor();
  await repeatedPanel.getByRole('button', { name: 'Publish exploration', exact: true }).click();
  await repeatedPanel.getByRole('button', { name: 'Unpublish', exact: true }).waitFor();
  assert.deepEqual(repeat.state.writes[0], repeat.state.writes[1]); await repeat.close();

  const refresh = await harness('loaded-detail-refresh-error-retry', { owned: true, failRefreshOnWrite: true });
  await refresh.page.goto(`${origin}/analyses/${outcomeId}`);
  const refreshedPanel = refresh.page.locator('.outcome-publication');
  await refreshedPanel.getByRole('button', { name: 'Publish exploration…', exact: true }).click();
  await refreshedPanel.getByRole('button', { name: 'Publish exploration', exact: true }).click();
  await refresh.page.getByText('The exploration could not be refreshed', { exact: true }).waitFor();
  assert.equal(await refresh.page.getByRole('heading', { name: gap.object.text }).count(), 1);
  await refreshedPanel.getByRole('button', { name: 'Unpublish', exact: true }).waitFor();
  await refresh.page.getByRole('button', { name: 'Retry', exact: true }).click();
  await until(async () => await refresh.page.getByText('The exploration could not be refreshed', { exact: true }).count() === 0, 'refresh error cleared');
  assert.equal(refresh.state.writes.length, 1); await refresh.close();

  const download = await harness('public-source-loopback-alias-and-untrusted-host');
  download.state.outcome.provenance.source_artifacts = [
    { file: { id: 'source-safe', filename: 'captured-source.json' }, download_url: 'http://localhost:3000/api/backend/v1/artifacts/' + 'a'.repeat(64) },
    { file: { id: 'source-unsafe', filename: 'untrusted-source.json' }, download_url: 'http://untrusted.example/api/backend/v1/artifacts/' + 'b'.repeat(64) }];
  await download.page.goto(`${origin}/analyses/${outcomeId}`);
  await download.page.getByText('Sources and analysis scope', { exact: true }).click();
  await download.page.getByText('Captured source evidence (2)', { exact: true }).click();
  assert.equal(await download.page.getByRole('link', { name: 'untrusted-source.json', exact: true }).count(), 0);
  const sourceLink = download.page.getByRole('link', { name: 'captured-source.json', exact: true });
  assert.equal(await sourceLink.getAttribute('href'), '/api/backend/v1/artifacts/' + 'a'.repeat(64));
  await Promise.all([download.page.waitForEvent('download'), sourceLink.click()]);
  assert.ok(!download.state.requests.some(request => request.path.includes('/jobs/'))); await download.close();

  const switched = await harness('private-outcome-hidden-on-identity-change', { owned: true, canClaim: true });
  await switched.page.goto(`${origin}/analyses/${outcomeId}`);
  await switched.page.getByRole('link', { name: 'View private activity', exact: true }).waitFor();
  await switched.page.getByRole('button', { name: 'Move my anonymous work', exact: true }).click();
  await until(() => switched.state.heldDetail.length > 0, 'new identity waits for its own record');
  assert.equal(await switched.page.locator('.outcome-page').count(), 0);
  assert.equal(await switched.page.locator('.outcome-publication').count(), 0);
  switched.state.holdDetail = false; switched.state.heldDetail.splice(0).forEach(resolve => resolve());
  await switched.page.getByText(switched.state.outcome.summary, { exact: true }).waitFor();
  assert.equal(await switched.page.getByRole('link', { name: 'View private activity', exact: true }).count(), 0);
  assert.equal(await switched.page.locator('.outcome-publication').count(), 0); await switched.close();
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.error = String(error); throw error; }
finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2)); await browser.close(); }
console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
