#!/usr/bin/env node
/** Local intercepted paragraph telemetry regression; no real jobs or model calls.
 * Optional PARAGRAPH_ACTIVITY_BASE_URL, PARAGRAPH_ACTIVITY_AUDIT_DIR,
 * PARAGRAPH_ACTIVITY_SCENARIO_FILTER, PLAYWRIGHT_MODULE, PLAYWRIGHT_EXECUTABLE_PATH.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.PARAGRAPH_ACTIVITY_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.PARAGRAPH_ACTIVITY_AUDIT_DIR || resolve(root, '.runtime/paragraph-activity-audit/browser'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const accountId = fixture.account.root_id, paragraph = fixture.paragraph.document.paragraphs.find(p => p.id === fixture.paragraph.root_id);
const userId = '11111111-1111-4111-8111-111111111111';
const firstId = '22222222-2222-4222-8222-222222222222', nextId = '33333333-3333-4333-8333-333333333333';
const now = '2026-09-28T14:00:00Z';
const job = (id, status = 'running') => ({ id, kind: 'paragraph', owner_user_id: userId, status,
  stage: 'authoring_paragraph', research_request_id: null, input_account_id: accountId, created_at: now, updated_at: now,
  completed_at: status === 'failed' ? now : null, result: null,
  failure: status === 'failed' ? { code: 'WORKER_FAILED', message: 'Fixture prior paragraph attempt failed.', retryable: true } : null,
  warnings: [], last_event_id: '0', links: { self: `/v1/jobs/${id}`, events: `/v1/jobs/${id}/events`, cancel: `/v1/jobs/${id}/cancel` } });
const detail = value => ({ kind: 'agent_message', state: 'started', source: 'harness', call_id: null, tool_name: null,
  selected_kg: null, display_arguments: null, output_excerpt: null, artifact_sha256: null, duration_ms: null, counts: null, ...value });
const citations = [...new Map(paragraph.citations.map(c => [`${c.target_id}@${c.citation_metadata_revision}`, c])).values()];
const rendering = { paragraph_id: paragraph.id, style: 'apa', locale: 'en-US',
  in_text: paragraph.citations.map((_, i) => ({ occurrence_index: i, label: `[${i + 1}]` })),
  bibliography: citations.map(c => ({ target_id: c.target_id, citation_metadata_revision: c.citation_metadata_revision,
    text: `Fixture reference: ${fixture.paragraph.citation_metadata.find(m => m.target_id === c.target_id)?.title || c.target_id}` })),
  rendering_manifest: { processor: 'browser-fixture', processor_version: '1', style_sha256: '0'.repeat(64), locale_sha256: '0'.repeat(64), citation_profile: 'reveal-citation-v1', metadata_checksums: [] } };
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(n => /^chromium-\d+$/.test(n)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* use Playwright default */ }
  }
}
async function until(check, message, timeout = 10000) { const started = performance.now(); while (!await check()) { assert.ok(performance.now() - started < timeout, message); await new Promise(r => setTimeout(r, 20)); } }
function gate() { let release; const promise = new Promise(r => { release = r; }); return { promise, release }; }
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Intercepted paragraph activity only: synthetic SSE, mock account/job/paragraph/citation responses and mocked retry/cancel requests. No real backend read, mutation, job, or model call.',
  fixtureContractSha256: fixture.contractSha256, baseUrl: origin, status: 'running', scenarios: [] };

async function harness(name, viewport, { failed = false, holdOld = false, submitFailures = 0 } = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const state = { jobs: { [firstId]: job(firstId, failed ? 'failed' : 'running'), [nextId]: job(nextId) },
    statement: { status: failed ? 'failed' : 'running', job_id: firstId, paragraph_id: null },
    requests: [], submissions: [], cancellations: [], errors: [], consoleErrors: [], unexpected: [], oldGate: gate(),
    firstJobReads: 0, oldDelivered: false, paragraphReads: 0, renders: 0, events: [], submitFailures };
  if (!holdOld) state.oldGate.release();
  page.on('pageerror', e => state.errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') state.consoleErrors.push(m.text()); });
  await context.exposeBinding('__paragraphSubmitFixture', async (_, request) => {
    state.submissions.push(request);
    assert.equal(request.body.kind, 'paragraph'); assert.equal(request.body.account_id, accountId);
    assert.ok(request.key, 'Paragraph retry includes an idempotency key');
    if (state.submitFailures-- > 0) return { status: 503, body: { code: 'SERVICE_UNAVAILABLE', detail: 'Fixture: submission confirmation is unavailable. Retry safely.' } };
    return { status: 200, body: state.jobs[nextId] };
  });
  await context.addInitScript(() => {
    const original = window.fetch.bind(window), encoder = new TextEncoder();
    const mock = { events: {}, streams: [], requests: [], expireNext: false, failNextJobRead: false, expiredResponses: 0, failedJobReads: 0, push(id, events) {
      (this.events[id] ||= []).push(...events);
      for (const stream of this.streams) if (stream.id === id && !stream.closed)
        for (const event of events) stream.controller.enqueue(encoder.encode(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`));
    }, finish(id) { for (const stream of this.streams) if (stream.id === id && !stream.closed) { stream.closed = true; stream.controller.close(); } } };
    window.__paragraphActivityFixture = mock;
    window.fetch = async (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      const method = init?.method || input?.method || 'GET';
      if (method === 'GET' && /^\/api\/backend\/v1\/jobs\/[^/]+$/.test(url.pathname) && mock.failNextJobRead) {
        mock.failNextJobRead = false; mock.failedJobReads++;
        return new Response(JSON.stringify({ code: 'SERVICE_UNAVAILABLE', detail: 'Fixture: job lookup briefly unavailable.' }), { status: 503, headers: { 'content-type': 'application/json' } });
      }
      if (url.pathname === '/api/backend/v1/jobs' && method === 'POST') {
        const request = input instanceof Request ? input : new Request(url, init);
        const response = await window.__paragraphSubmitFixture({ body: await request.clone().json(), key: request.headers.get('Idempotency-Key') });
        return new Response(JSON.stringify(response.body), { status: response.status, headers: { 'content-type': 'application/json' } });
      }
      const match = url.pathname.match(/^\/api\/backend\/v1\/jobs\/([^/]+)\/events$/);
      if (!match) return original(input, init);
      const id = match[1]; mock.requests.push({ id, after: url.searchParams.get('after') });
      if (mock.expireNext) {
        mock.expireNext = false; mock.failNextJobRead = true; mock.expiredResponses++;
        return new Response(JSON.stringify({ code: 'EVENT_CURSOR_EXPIRED', detail: 'Fixture: prior replay cursor expired.' }), { status: 410, headers: { 'content-type': 'application/json' } });
      }
      const stream = { id, controller: null, closed: false };
      const body = new ReadableStream({ start(controller) { stream.controller = controller; }, cancel() { stream.closed = true; } });
      mock.streams.push(stream);
      const after = BigInt(url.searchParams.get('after') || '0');
      for (const event of mock.events[id] || []) if (BigInt(event.id) > after) stream.controller.enqueue(encoder.encode(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`));
      const signal = init?.signal || input?.signal;
      signal?.addEventListener('abort', () => { if (!stream.closed) { stream.closed = true; stream.controller.close(); } }, { once: true });
      return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
    };
  });
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), path = decodeURIComponent(url.pathname), method = req.method();
    if (url.origin !== origin) { state.unexpected.push(`${method} external ${url.origin}`); return route.abort(); }
    if (!path.startsWith('/api/')) { assert.equal(method, 'GET'); return route.continue(); }
    state.requests.push({ path, method });
    const send = body => route.fulfill({ status: 200, json: body });
    if (method === 'GET' && path === '/api/session/status') return send({ principal: { user_id: userId }, canClaim: false, providers: { google: false, orcid: false } });
    if (method === 'GET' && path === '/api/backend/v1/me') return send({ user_id: userId, display_name: 'Paragraph fixture', principal_kind: 'anonymous', workspace_expires_at: null, email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null });
    if (method === 'GET' && path === `/api/backend/v1/accounts/${accountId}`) return send({ ...fixture.account, research_statement: state.statement });
    if (method === 'GET' && path === `/api/backend/v1/paragraphs/${paragraph.id}`) { state.paragraphReads++; return send(fixture.paragraph); }
    if (method === 'POST' && path === '/api/backend/v1/citations/render') { assert.equal(req.postDataJSON().paragraph_id, paragraph.id); state.renders++; return send(rendering); }
    for (const id of [firstId, nextId]) {
      if (method === 'GET' && path === `/api/backend/v1/jobs/${id}`) {
        if (id === firstId) { state.firstJobReads++; await state.oldGate.promise; state.oldDelivered = true; }
        return send(state.jobs[id]).catch(() => {});
      }
      if (method === 'POST' && path === `/api/backend/v1/jobs/${id}/cancel`) {
        state.cancellations.push(id); state.jobs[id] = { ...state.jobs[id], status: 'cancel_requested' }; return send(state.jobs[id]);
      }
    }
    state.unexpected.push(`${method} ${path}`); return route.fulfill({ status: 500, json: { code: 'UNEXPECTED_TEST_ROUTE', detail: 'Blocked unmocked API route.' } });
  });
  const result = { name, viewport, checks: [] }; report.scenarios.push(result);
  const next = {};
  return { page, state, result,
    event(id, message, stage = 'authoring_paragraph', extra = {}) { const n = next[id] = (next[id] || 0) + 1; return { id: String(n), job_id: id, occurred_at: new Date(Date.parse(now) + n * 1000).toISOString(), event_type: 'activity', status: 'running', stage, message, result: null, detail: detail({}), ...extra }; },
    async open() { await page.goto(`${origin}/accounts/${encodeURIComponent(accountId)}?view=statement`); await page.getByRole('tab', { name: 'Research Statement', exact: true }).waitFor(); },
    async stream(id) { await page.waitForFunction(id => window.__paragraphActivityFixture.streams.some(s => s.id === id && !s.closed), id); },
    async emit(id, events) { state.events.push(...events); const last = events.at(-1); state.jobs[id] = { ...state.jobs[id], status: last.status, stage: last.stage, result: last.result, last_event_id: last.id }; await page.evaluate(({ id, events }) => window.__paragraphActivityFixture.push(id, events), { id, events }); },
    async close() { state.oldGate.release(); result.events = state.events.length; result.submissions = state.submissions; result.cancellations = state.cancellations;
      result.paragraphReads = state.paragraphReads; result.citationRenders = state.renders; result.pageErrors = state.errors; result.consoleErrors = state.consoleErrors; result.unexpectedRequests = state.unexpected;
      result.streamRequests = await page.evaluate(() => window.__paragraphActivityFixture.requests);
      result.transportFaults = await page.evaluate(() => ({ expiredResponses: window.__paragraphActivityFixture.expiredResponses, failedJobReads: window.__paragraphActivityFixture.failedJobReads }));
      await context.close(); },
  };
}
async function screenshot(h, label) { await h.page.screenshot({ path: resolve(output, `${h.result.name}-${label}.png`), fullPage: true }); }
async function healthy(h) { assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.consoleErrors, []); assert.deepEqual(h.state.unexpected, []); assert.ok(await h.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)); }
async function complete(h, id) {
  stateComplete(h, id);
  await h.emit(id, [h.event(id, 'Fixture paragraph accepted after checks.', 'complete', { event_type: 'result', status: 'succeeded', result: { kind: 'paragraph', account_id: accountId, paragraph_id: paragraph.id }, detail: null })]);
  await h.page.evaluate(id => window.__paragraphActivityFixture.finish(id), id);
  await h.page.locator('.research-prose').waitFor();
  assert.ok((await h.page.locator('.research-prose').innerText()).includes(paragraph.text.slice(0, 90)));
  assert.ok(h.state.paragraphReads > 0 && h.state.renders > 0, 'Accepted paragraph and citation rendering load automatically');
  assert.equal(await h.page.getByRole('button', { name: 'Retry paragraph generation', exact: true }).count(), 0);
  await healthy(h);
}
function stateComplete(h, id) { h.state.statement = { status: 'succeeded', job_id: id, paragraph_id: paragraph.id }; }
async function liveScenario(name, viewport) {
  const h = await harness(name, viewport);
  try {
    await h.open(); await h.stream(firstId);
    await h.page.getByRole('heading', { name: 'Statement activity', exact: true }).waitFor();
    await h.emit(firstId, [...Array.from({ length: 28 }, (_, i) => h.event(firstId, `Fixture paragraph update ${i + 1}: inspecting a frozen accepted claim without adding scientific assertions.`)),
      h.event(firstId, 'Fixture paragraph writer: checking the accepted claims.'),
      h.event(firstId, 'Using Read', 'authoring_paragraph', { detail: detail({ kind: 'tool_call', tool_name: 'Read', call_id: 'paragraph-read', display_arguments: '{"file_path":"input/paragraph-input.json"}' }) }),
      h.event(firstId, 'Read returned', 'authoring_paragraph', { detail: detail({ kind: 'tool_result', state: 'completed', tool_name: 'Read', call_id: 'paragraph-read', output_excerpt: 'FIXTURE_PARAGRAPH_SOURCE_DETAILS' }) })]);
    await h.page.getByText('Fixture paragraph writer: checking the accepted claims.', { exact: true }).waitFor();
    const tool = h.page.locator('.paragraph-activity .activity-tool'); await tool.waitFor();
    const history = h.page.getByRole('region', { name: 'Activity history', exact: true });
    const dimensions = await history.evaluate(el => ({ height: el.clientHeight, total: el.scrollHeight, viewport: innerHeight }));
    assert.ok(dimensions.height >= 160 && dimensions.height < dimensions.viewport && dimensions.total > dimensions.height * 2, 'Paragraph history stays bounded for a genuinely long stream');
    await history.hover(); await h.page.mouse.wheel(0, -1200);
    await h.page.getByRole('button', { name: 'Jump to latest', exact: true }).click();
    assert.equal(await tool.getAttribute('open'), null); await tool.locator(':scope > summary').click();
    await tool.getByText('FIXTURE_PARAGRAPH_SOURCE_DETAILS', { exact: true }).waitFor();
    await screenshot(h, 'live-tools');
    await h.emit(firstId, [h.event(firstId, 'Fixture validating paragraph faithfulness.', 'validating', { detail: detail({ kind: 'validation', source: 'validator' }) })]);
    await h.page.getByText('Checking claims and citations', { exact: true }).waitFor();
    assert.equal(await h.page.locator('.research-prose').count(), 0);
    await complete(h, firstId); await screenshot(h, 'ready');
    assert.equal(h.state.submissions.length, 0);
    h.result.checks.push('Running paragraph exposes bounded long history, tail jump, live agent messages, compact expandable tool and validation stage; terminal success automatically loads exact saved paragraph and citations');
  } finally { await h.close(); }
}
async function cancelRetryScenario() {
  const h = await harness('cancel-retry-mobile', { width: 390, height: 844 });
  try {
    await h.open(); await h.stream(firstId);
    await h.page.getByRole('button', { name: 'Stop', exact: true }).click();
    await h.page.getByRole('button', { name: 'Stopping…', exact: true }).waitFor();
    assert.equal(await h.page.getByRole('button', { name: 'Retry paragraph generation', exact: true }).count(), 0);
    h.state.statement = { status: 'cancelled', job_id: firstId, paragraph_id: null };
    await h.emit(firstId, [h.event(firstId, 'Fixture paragraph stopped.', 'authoring_paragraph', { status: 'cancelled', event_type: 'status' })]);
    await h.page.evaluate(id => window.__paragraphActivityFixture.finish(id), firstId);
    await h.page.getByRole('button', { name: 'Retry paragraph generation', exact: true }).click();
    await h.stream(nextId);
    await h.emit(nextId, [h.event(nextId, 'Fixture replacement paragraph is running.')]);
    await h.page.getByText('Fixture replacement paragraph is running.', { exact: true }).waitFor();
    await complete(h, nextId); assert.deepEqual(h.state.cancellations, [firstId]); assert.equal(h.state.submissions.length, 1);
    h.result.checks.push('Cancel shows stopping until terminal; one explicit retry attaches the returned new job despite stale account status and then loads its paragraph');
  } finally { await h.close(); }
}
async function failedRetryRaceScenario() {
  const h = await harness('failed-retry-stale-job', { width: 1280, height: 900 }, { failed: true, holdOld: true, submitFailures: 1 });
  try {
    await h.open(); await until(() => h.state.firstJobReads > 0, 'held old job read');
    const retry = h.page.getByRole('button', { name: 'Retry paragraph generation', exact: true }); await retry.click();
    await h.page.getByRole('alert').filter({ hasText: 'Fixture: submission confirmation is unavailable.' }).waitFor();
    await retry.click(); await h.stream(nextId);
    assert.equal(h.state.submissions.length, 2); assert.equal(h.state.submissions[0].key, h.state.submissions[1].key, 'Uncertain submission retry preserves its idempotency key');
    await h.emit(nextId, [h.event(nextId, 'Fixture current job must win over a late old job read.')]);
    await h.page.getByText('Fixture current job must win over a late old job read.', { exact: true }).waitFor();
    h.state.oldGate.release(); await until(() => h.state.oldDelivered, 'late old job response delivered');
    await h.page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    await h.page.getByText('Fixture current job must win over a late old job read.', { exact: true }).waitFor();
    assert.equal(await h.page.getByText('Fixture prior paragraph attempt failed.', { exact: true }).count(), 0, 'Old job cannot replace the current retry activity');
    assert.equal(await h.page.evaluate(id => window.__paragraphActivityFixture.streams.filter(s => s.id === id && !s.closed).length, firstId), 0);
    await complete(h, nextId); await screenshot(h, 'recovered');
    h.result.checks.push('Unknown submit failure retries with the same key; a late previous-job fetch cannot overwrite new activity; the replacement succeeds');
  } finally { await h.close(); }
}
async function savedStatementScenario() {
  const h = await harness('saved-statement-deferred', { width: 1280, height: 900 });
  try {
    stateComplete(h, firstId);
    h.state.jobs[firstId] = { ...h.state.jobs[firstId], status: 'succeeded', stage: 'complete', completed_at: now,
      result: { kind: 'paragraph', account_id: accountId, paragraph_id: paragraph.id } };
    await h.open(); await h.page.locator('.research-prose').waitFor();
    const show = h.page.getByRole('button', { name: 'View statement activity', exact: false }); await show.waitFor();
    assert.equal(h.state.firstJobReads, 0, 'Saved statement does not fetch its completed job until requested');
    assert.equal(await h.page.evaluate(() => window.__paragraphActivityFixture.requests.length), 0);
    await show.click(); await h.stream(firstId);
    await h.page.getByRole('button', { name: 'Research statement ready', exact: false }).waitFor();
    assert.ok(h.state.firstJobReads > 0);
    assert.equal(h.state.submissions.length, 0); await healthy(h);
    h.result.checks.push('Saved paragraph and citations load without job/event reads; View statement activity explicitly opens completed telemetry without starting new generation');
  } finally { await h.close(); }
}
async function cursorRecoveryScenario() {
  const h = await harness('cursor-expired-job-recovery', { width: 1280, height: 900 });
  try {
    await h.page.clock.install(); await h.open(); await h.stream(firstId);
    const initialReads = h.state.firstJobReads;
    await h.page.evaluate(id => { window.__paragraphActivityFixture.expireNext = true; window.__paragraphActivityFixture.finish(id); }, firstId);
    await until(() => h.state.firstJobReads > initialReads, 'job refresh after fixture stream closure');
    await h.page.clock.fastForward(1050);
    await h.page.waitForFunction(() => window.__paragraphActivityFixture.failedJobReads === 1);
    await h.page.getByText('Connection interrupted. Reconnecting…', { exact: true }).waitFor();
    await h.page.clock.fastForward(4050); await h.stream(firstId);
    await h.emit(firstId, [h.event(firstId, 'Fixture activity recovered after expired cursor and failed job lookup.')]);
    await h.page.getByText('Fixture activity recovered after expired cursor and failed job lookup.', { exact: true }).waitFor();
    await complete(h, firstId);
    assert.ok(h.state.firstJobReads > initialReads + 1, 'A later job GET succeeds after the injected failed lookup');
    assert.equal(await h.page.evaluate(() => window.__paragraphActivityFixture.expiredResponses), 1);
    await screenshot(h, 'recovered');
    h.result.checks.push('Expired replay cursor followed by failed job lookup backs off and reconnects; later job read and paragraph completion succeed without an unhandled error');
  } finally { await h.close(); }
}
try {
  const filter = process.env.PARAGRAPH_ACTIVITY_SCENARIO_FILTER ? new RegExp(process.env.PARAGRAPH_ACTIVITY_SCENARIO_FILTER) : null;
  const scenarios = [['live-desktop', () => liveScenario('live-desktop', { width: 1280, height: 900 })],
    ['live-mobile', () => liveScenario('live-mobile', { width: 390, height: 844 })],
    ['cancel-retry-mobile', cancelRetryScenario], ['failed-retry-stale-job', failedRetryRaceScenario],
    ['saved-statement-deferred', savedStatementScenario], ['cursor-expired-job-recovery', cursorRecoveryScenario]].filter(([name]) => !filter || filter.test(name));
  assert.ok(scenarios.length); for (const [, run] of scenarios) await run(); report.status = 'passed';
} catch (error) { report.status = 'failed'; report.failure = error.stack || error.message; process.exitCode = 1; }
finally { await browser.close(); await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report, null, 2)); }
