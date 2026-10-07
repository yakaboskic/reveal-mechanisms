#!/usr/bin/env node
/** Intercepted activity regression; no real authentication, API writes, or jobs.
 * Run against the local frontend: node services/frontend/scripts/check-activity.mjs
 * Optional ACTIVITY_BASE_URL, ACTIVITY_AUDIT_DIR, ACTIVITY_SCENARIO_FILTER,
 * PLAYWRIGHT_MODULE, PLAYWRIGHT_EXECUTABLE_PATH. Playwright is an external test tool.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.ACTIVITY_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Use a local frontend only');
const output = resolve(process.env.ACTIVITY_AUDIT_DIR || resolve(root, '.runtime/activity-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0];
const factor = fixture.suggestions.automatic_anchors[0].factor;
const userId = '11111111-1111-4111-8111-111111111111';
const draftId = '22222222-2222-4222-8222-222222222222';
const jobId = '33333333-3333-4333-8333-333333333333';
const requestId = '44444444-4444-4444-8444-444444444444';
const now = '2026-09-28T12:00:00Z';
const composer = {
  source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision },
  eaggl_anchors: [{ reference: { source: 'eaggl', source_id: factor.source_id,
    source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }],
  dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg', 'prokn'],
};
const draft = { id: draftId, owner_user_id: userId, version: 1, composer, created_at: now, updated_at: now };
const initialJob = {
  id: jobId, kind: 'analysis', owner_user_id: userId, status: 'running', stage: 'preparing_evidence',
  research_request_id: requestId, input_account_id: null, created_at: now, updated_at: now,
  completed_at: null, result: null, failure: null, warnings: [], last_event_id: '0',
  links: { self: `/v1/jobs/${jobId}`, events: `/v1/jobs/${jobId}/events`, cancel: `/v1/jobs/${jobId}/cancel` },
};
const detail = overrides => ({ kind: 'agent_message', state: 'started', source: 'harness', call_id: null,
  tool_name: null, selected_kg: null, display_arguments: null, output_excerpt: null,
  artifact_sha256: null, duration_ms: null, counts: null, ...overrides });
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
    try { await access(path); return path; } catch { /* use Playwright default */ }
  }
}
async function until(check, description, timeout = 10000) {
  const start = performance.now();
  while (!await check()) {
    assert.ok(performance.now() - start < timeout, `Timed out: ${description}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}
function gate() { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; }
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked browser activity only: controlled in-browser SSE; all other API requests intercepted; no real login, research job, token count, or database write.',
  fixtureContractSha256: fixture.contractSha256, baseUrl: origin, status: 'running', scenarios: [] };

async function harness(name, viewport) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const state = { job: structuredClone(initialJob), gap: structuredClone(gap), gapGate: gate(),
    events: [], requests: [], unexpected: [], errors: [], consoleErrors: [], duplicateKeyWarnings: [] };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('console', message => {
    const text = message.text();
    if (message.type() === 'error') state.consoleErrors.push(text);
    if (/same key|unique ["']?key["']? prop|duplicate.{0,30}key|two children with/i.test(text)) state.duplicateKeyWarnings.push(text);
  });
  await context.addInitScript(snapshot => {
    sessionStorage.setItem('reveal:composer', JSON.stringify(snapshot));
    const original = window.fetch.bind(window); const encoder = new TextEncoder();
    const mock = { events: [], streams: [], requests: [], push(events) {
      this.events.push(...events);
      for (const stream of this.streams) if (!stream.closed) {
        for (const event of events) {
          const bytes = encoder.encode(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`);
          // Exercise the production SSE parser across arbitrary network boundaries.
          const middle = Math.floor(bytes.length / 2);
          stream.controller.enqueue(bytes.slice(0, middle)); stream.controller.enqueue(bytes.slice(middle));
        }
      }
    }, finish() {
      for (const stream of this.streams) if (!stream.closed) { stream.closed = true; stream.controller.close(); }
    } };
    window.__activityFixture = mock;
    window.fetch = async (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (url.pathname === '/api/backend/v1/me/workspace/events') {
        let closed = false;
        const body = new ReadableStream({ start(controller) {
          controller.enqueue(encoder.encode(': isolated workspace fixture\n\n'));
          init?.signal?.addEventListener('abort', () => { if (!closed) { closed = true; controller.close(); } }, { once: true });
        }, cancel() { closed = true; } });
        return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
      }
      if (!/^\/api\/backend\/v1\/jobs\/[^/]+\/events$/.test(url.pathname)) return original(input, init);
      mock.requests.push({ path: url.pathname, after: url.searchParams.get('after'), lastEventId: new Headers(init?.headers).get('Last-Event-ID') });
      const stream = { controller: null, closed: false };
      const body = new ReadableStream({ start(controller) { stream.controller = controller; }, cancel() { stream.closed = true; } });
      mock.streams.push(stream);
      const after = BigInt(url.searchParams.get('after') || '0');
      for (const event of mock.events) if (BigInt(event.id) > after)
        stream.controller.enqueue(encoder.encode(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`));
      init?.signal?.addEventListener('abort', () => { if (!stream.closed) { stream.closed = true; stream.controller.close(); } }, { once: true });
      return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
    };
  }, { composer, gap, factors: { [factor.source_id]: factor }, draft, owner: userId, job: initialJob });
  await context.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (url.origin !== origin) { state.unexpected.push(`${request.method()} external ${url.origin}${path}`); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/')) {
      assert.equal(request.method(), 'GET', 'No non-API writes'); return route.continue();
    }
    state.requests.push({ method: request.method(), path });
    const respond = json => route.fulfill({ status: 200, json });
    if (request.method() !== 'GET') { state.unexpected.push(`${request.method()} ${path}`); return route.abort('blockedbyclient'); }
    if (path === '/api/session/status') { if (state.sessionGate) await state.sessionGate.promise; return respond({ principal: { user_id: userId }, canClaim: false, providers: { google: true, orcid: true } }); }
    if (path === '/api/backend/v1/me') return respond({ user_id: userId, display_name: 'Activity browser fixture', email: null,
      email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'anonymous', workspace_expires_at: null });
    if (path === `/api/backend/v1/jobs/${jobId}`) { if (state.jobGate) await state.jobGate.promise; return respond(state.job); }
    if (path === `/api/backend/v1/research-requests/${requestId}`) return respond({ id: requestId, owner_user_id: userId,
      source_draft_id: draftId, source_draft_version: 1, composer, question_id: gap.object.id,
      document: { knowledge_gaps: [gap.object] }, attribution: { user_id: userId, person_id: null, display_name: 'Activity browser fixture',
        orcid: null, orcid_authenticated: false, observed_at: now, principal_kind: 'anonymous' }, submitted_at: now, linked_dismech_context: [] });
    if (path === `/api/backend/v1/drafts/${draftId}`) return respond(draft);
    if (path === '/api/backend/v1/knowledge-gaps') return respond(fixture.gaps);
    if (path === '/api/backend/v1/knowledge-gaps/search') return respond({ items: [], next_cursor: null });
    if (path.startsWith('/api/backend/v1/knowledge-gaps/')) { await state.gapGate.promise; return respond(state.gap); }
    if (path.startsWith('/api/backend/v1/mechanisms/')) return respond(factor);
    state.unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 500, json: { code: 'UNEXPECTED_TEST_ROUTE', detail: 'Blocked unmocked API route.' } });
  });
  const result = { name, viewport, checks: [] }; report.scenarios.push(result);
  let next = 0;
  const event = (message, stage = 'authoring_account', extra = {}) => ({ id: String(++next), job_id: jobId,
    occurred_at: new Date(Date.parse(now) + next * 1000).toISOString(), event_type: 'activity', status: 'running',
    stage, message, result: null, detail: detail({}), ...extra });
  return { page, state, result, event,
    async open() {
      await page.goto(`${origin}/?draft=${draftId}&job=${jobId}`);
      await page.getByRole('region', { name: 'Research activity', exact: true }).waitFor();
      await page.waitForFunction(() => window.__activityFixture.streams.some(stream => !stream.closed));
    },
    async emit(events, { advanceJob = true } = {}) {
      state.events.push(...events); const last = events.at(-1);
      if (advanceJob) state.job = { ...state.job, status: last.status, stage: last.stage, last_event_id: last.id, updated_at: last.occurred_at };
      await page.evaluate(events => window.__activityFixture.push(events), events);
    },
    async close() {
      state.gapGate.release(); state.sessionGate?.release(); state.jobGate?.release();
      result.eventCount = state.events.length; result.pageErrors = state.errors; result.blockedUnexpectedRequests = state.unexpected;
      result.consoleErrors = state.consoleErrors; result.duplicateKeyWarnings = state.duplicateKeyWarnings;
      result.apiMethods = [...new Set(state.requests.map(request => request.method))];
      await context.close();
    },
  };
}
const history = h => h.page.getByRole('region', { name: 'Activity history', exact: true });
const metrics = h => history(h).evaluate(el => ({ top: el.scrollTop, height: el.clientHeight, total: el.scrollHeight,
  remaining: el.scrollHeight - el.scrollTop - el.clientHeight, overflowY: getComputedStyle(el).overflowY,
  pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth }));
async function atTail(h) { await until(async () => (await metrics(h)).remaining < 5, 'activity follows latest event'); }
async function stage(h, name, status = 'working') {
  const active = h.page.locator(`.activity-stage[data-stage="${name}"][data-state="${status}"]`);
  await active.waitFor();
  assert.equal(await h.page.locator('.activity-stage[data-state="working"]').count(), status === 'working' ? 1 : 0);
  assert.equal(await h.page.locator('.activity-stage .pulse').count(), status === 'working' ? 1 : 0);
  h.result.checks.push(`${name} stage is ${status}; ${status === 'working' ? 'only the active stage pulses' : 'no stage pulses remain'}`);
}
async function screenshot(h, suffix) { await h.page.screenshot({ path: resolve(output, `${h.result.name}-${suffix}.png`), fullPage: true }); }

async function activityScenario(name, viewport) {
  const h = await harness(name, viewport);
  try {
    await h.open();
    await h.emit([h.event('Fixture: collecting captured observations.', 'preparing_evidence', { detail: detail({ kind: 'preparation', source: 'worker' }) })]);
    await stage(h, 'preparation');
    await h.emit([h.event('Fixture: preparing the agent workspace.', 'starting_agent', { detail: detail({ kind: 'preparation', source: 'worker' }) })]);
    await stage(h, 'setup');
    const log = [];
    for (let i = 0; i < 60; i++) {
      log.push(h.event(`Fixture update ${i + 1}: reading a selected observation and checking its exact source. No scientific result is asserted.`));
      log.push(h.event('Using Read', 'authoring_account', { detail: detail({ kind: 'tool_call', tool_name: 'Read', call_id: `read-${i}`, display_arguments: JSON.stringify({ file_path: `input/evidence-records/fixture-${i}.json`, offset: 1, limit: 100 }) }) }));
      log.push(h.event('Read completed', 'authoring_account', { detail: detail({ kind: 'tool_result', state: 'completed', tool_name: 'Read', call_id: `read-${i}`, duration_ms: 120 + i, output_excerpt: `Fixture output ${i + 1}: exact source row retained for review.` }) }));
    }
    log.push(h.event('Incremental ', 'authoring_account', { detail: detail({ message_delta: true }) }),
      h.event('public update ', 'authoring_account', { detail: detail({ message_delta: true }) }),
      h.event('stays readable.', 'authoring_account', { detail: detail({ message_delta: true }) }));
    await h.emit(log);
    await stage(h, 'research'); await atTail(h);
    assert.equal(await h.page.getByText('Incremental public update stays readable.', { exact: true }).count(), 1);
    const size = await metrics(h);
    assert.ok(size.height >= 160 && size.height <= Math.min(viewport.height, 660), 'Long log has bounded visible height');
    assert.ok(size.total > size.height * 3, 'Long fixture actually overflows the scroll region');
    assert.ok(['auto', 'scroll'].includes(size.overflowY)); assert.ok(size.pageWidth <= size.viewportWidth + 1, 'No horizontal page overflow');
    h.result.longLog = size;
    h.result.checks.push('Long mixed log is bounded; explicit text deltas coalesce; viewport has no horizontal overflow');
    await screenshot(h, 'following');
    {
      h.state.gap.object.text = `${gap.object.text} ${'Fixture delayed question context changes the panel position. '.repeat(5)}`;
      h.state.gapGate.release();
      await until(async () => (await h.page.locator('.selected-question').innerText()).includes('Fixture delayed question'), 'delayed question moves the panel');
      await atTail(h);
      const moved = await metrics(h);
      assert.ok(moved.height >= 160 && moved.height <= Math.min(viewport.height, 660));
      h.result.delayedQuestion = moved;
      h.result.checks.push('Delayed question rendering recomputes the activity viewport and retains tail following');
    }
    await h.page.setViewportSize({ width: viewport.width, height: Math.max(600, viewport.height - 180) });
    await atTail(h);
    const resized = await metrics(h);
    assert.ok(resized.height <= Math.max(600, viewport.height - 180) * .65 + 5, 'Resizing keeps the log within the viewport allocation');
    await h.page.setViewportSize(viewport); await atTail(h);
    h.result.checks.push('Resizing the browser recomputes bounded height without losing tail following');
    await h.emit([h.event('Tail marker A: appended while following.')]); await atTail(h);
    await history(h).hover(); await h.page.mouse.wheel(0, -1600);
    await until(async () => (await metrics(h)).remaining > 100, 'scroll-up pauses tail following');
    const jump = h.page.getByRole('button', { name: 'Jump to latest', exact: true }); await jump.waitFor();
    const paused = await metrics(h);
    await h.emit([h.event('Tail marker B: appended while paused.'), h.event('Tail marker C: remains below the current reading position.')]);
    await h.page.getByText('Tail marker C: remains below the current reading position.', { exact: true }).waitFor({ state: 'attached' });
    assert.ok(Math.abs((await metrics(h)).top - paused.top) < 3, 'Appending does not move the paused reading position');
    await screenshot(h, 'paused');
    await jump.click(); await atTail(h);
    await h.emit([h.event('Tail marker D: following resumes after Jump to latest.')]); await atTail(h);
    h.result.checks.push('Append follows tail; scrolling up pauses; Jump to latest resumes following');
    const fullOutput = 'Fixture detailed result\n' + 'Long_unbroken_identifier_'.repeat(60) + '\nFINAL_DETAIL_MARKER';
    await h.emit([
      h.event('Using Grep', 'authoring_account', { detail: detail({ kind: 'tool_call', tool_name: 'Grep', call_id: 'inspect-detail', display_arguments: '{"pattern":"exact/source~identity","path":"input/evidence-records"}' }) }),
      h.event('Grep completed', 'authoring_account', { detail: detail({ kind: 'tool_result', state: 'completed', tool_name: 'Grep', call_id: 'inspect-detail', duration_ms: 234, output_excerpt: fullOutput }) }),
    ]); await atTail(h);
    const tool = h.page.locator('.activity-tool').filter({ hasText: 'Grep' }).last(); await tool.waitFor();
    assert.equal(await tool.getAttribute('open'), null, 'Tool invocation starts collapsed');
    const body = tool.locator('.tool-details-body');
    assert.equal(await body.isVisible(), false);
    await tool.locator(':scope > summary').click();
    await until(() => body.isVisible(), 'tool invocation disclosure');
    assert.ok((await body.innerText()).includes('FINAL_DETAIL_MARKER'));
    assert.equal(await h.page.locator('.activity-tool').filter({ hasText: 'Grep' }).count(), 1, 'Call and result are shown as one paired tool card');
    assert.ok((await metrics(h)).pageWidth <= viewport.width + 1, 'Expanded tool detail wraps on narrow screens');
    await jump.waitFor();
    h.result.checks.push('Expanding a tool pauses auto-follow for inspection');
    await jump.click(); await atTail(h);
    await h.emit([h.event('Tail marker E: follows after expanding a long tool result.')]); await atTail(h);
    await screenshot(h, 'tool-details');
    h.result.checks.push('Paired tool invocation keeps arguments and result collapsed until requested and preserves the full recorded excerpt');
    await h.emit([h.event('Fixture: checking the draft against captured evidence.', 'validating', { detail: detail({ kind: 'validation', source: 'validator' }) })]);
    await stage(h, 'validation');
    await h.page.emulateMedia({ reducedMotion: 'reduce' });
    const animations = await h.page.locator('.activity-stage .pulse i').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).animationName));
    assert.ok(animations.length > 0 && animations.every(name => name === 'none'), 'Reduced motion disables stage pulse animation');
    await screenshot(h, 'validation-reduced-motion');
    h.result.checks.push('Validation stage supersedes research directly from the open SSE stream; reduced motion disables animation');
    h.state.job.failure = { code: 'VALIDATION_FAILED', message: 'Fixture validation stopped: no scientific account was accepted.', retryable: true };
    h.state.job.completed_at = now;
    await h.emit([h.event('Fixture validation could not complete.', 'validating', { status: 'failed', event_type: 'failure', detail: detail({ kind: 'validation', state: 'failed', source: 'validator' }) })]);
    await h.page.evaluate(() => window.__activityFixture.finish());
    await stage(h, 'validation', 'failed');
    await h.page.getByRole('alert').filter({ hasText: 'Fixture validation stopped' }).waitFor();
    const terminalSize = await metrics(h);
    assert.ok(terminalSize.height <= Math.min(viewport.height, 660), 'Terminal history remains bounded');
    assert.ok(['auto', 'scroll'].includes(terminalSize.overflowY));
    assert.equal(await h.page.getByRole('button', { name: 'Stop', exact: true }).count(), 0);
    assert.equal(await h.page.getByText('Gap analysis complete', { exact: false }).count(), 0);
    await screenshot(h, 'terminal');
    h.result.checks.push('Terminal failure stops pulses without claiming acceptance; retained history remains bounded');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
    assert.ok(h.state.requests.every(request => request.method === 'GET'));
  } finally { await h.close(); }
}
async function replayAheadScenario() {
  const h = await harness('replay-ahead', { width: 1280, height: 900 });
  try {
    h.state.job = { ...h.state.job, stage: 'validating', last_event_id: '50' };
    h.state.gapGate.release();
    await h.open();
    await stage(h, 'validation');
    await h.emit([h.event('Fixture replay: an earlier research update is still arriving.')], { advanceJob: false });
    await h.page.getByText('Fixture replay: an earlier research update is still arriving.', { exact: true }).waitFor();
    await stage(h, 'validation');
    const research = h.page.locator('.activity-stage[data-stage="research"]');
    assert.equal(await research.getAttribute('data-state'), 'ended');
    assert.equal(await research.locator('.pulse').count(), 0, 'Older replayed research must not regain the active pulse');
    assert.equal(await h.page.locator('.activity-stage').last().getAttribute('data-stage'), 'validation');
    h.result.checks.push('Fetched job validation at cursor 50 stays active while research event 1 replays; the older stage has no pulse');
    await screenshot(h, 'validation-during-replay');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
    assert.ok(h.state.requests.every(request => request.method === 'GET'));
  } finally { await h.close(); }
}
async function outputCollectionScenario(name, viewport) {
  const h = await harness(name, viewport);
  try {
    h.state.job = { ...h.state.job, stage: 'authoring_account' };
    h.state.gapGate.release();
    await h.open();
    const base = Date.now() - 12000;
    const time = seconds => new Date(base + seconds * 1000).toISOString();
    const prose = 'The agent has finished its interpretation; the causal question remains open.';
    await h.emit([h.event(prose, 'authoring_account', { occurred_at: time(0) })], { advanceJob: false });
    await stage(h, 'research');
    await h.emit([h.event('Collecting the finished output and captured evidence.', 'collecting_output', {
      occurred_at: time(10), detail: detail({ kind: 'preparation', source: 'worker' }),
    })], { advanceJob: false });
    await stage(h, 'collection');
    const research = h.page.locator('.activity-stage[data-stage="research"]');
    const collection = h.page.locator('.activity-stage[data-stage="collection"]');
    assert.equal(await research.getAttribute('data-state'), 'ended');
    assert.equal(await research.locator('.stage-state').innerText(), 'Ended');
    assert.equal(await research.locator('.stage-duration').innerText(), '10s');
    assert.equal(await research.locator('.pulse').count(), 0);
    assert.equal(await collection.locator('.stage-name').innerText(), 'Preserving output and evidence');
    await h.page.getByText(prose, { exact: true }).waitFor();
    const timer = collection.locator('.stage-duration');
    const before = await timer.innerText();
    await until(async () => await timer.innerText() !== before, 'collection timer advances while the job snapshot is still authoring');
    assert.equal(await research.locator('.stage-duration').innerText(), '10s');
    assert.equal(await h.page.getByText('Scientific accounts ready', { exact: true }).count(), 0);
    await screenshot(h, 'collecting-output');
    await h.emit([h.event('Validating source fidelity and scientific identities.', 'validating', {
      occurred_at: time(12), detail: detail({ kind: 'validation', source: 'validator' }),
    })], { advanceJob: false });
    await stage(h, 'validation');
    assert.equal(await collection.getAttribute('data-state'), 'completed');
    assert.equal(await collection.locator('.stage-duration').innerText(), '2s');
    assert.equal(await research.locator('.stage-duration').innerText(), '10s');
    await h.page.getByText(prose, { exact: true }).waitFor();
    assert.equal(h.state.job.stage, 'authoring_account', 'SSE alone advances every displayed stage');
    h.result.checks.push('Final agent prose remains visible; research ends at handoff without implying success; preservation becomes the only working section before any refreshed job snapshot');
    h.result.checks.push('Collection timer advances independently, then stops at scientific validation without claiming the job is accepted');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
    assert.ok(h.state.requests.every(request => request.method === 'GET'));
  } finally { await h.close(); }
}
async function failedCaptureScenario(name, viewport, early) {
  const h = await harness(name, viewport);
  try {
    h.state.job = { ...h.state.job, stage: 'authoring_account' };
    h.state.gapGate.release(); await h.open();
    const base = Date.now() - 960000;
    const time = seconds => new Date(base + seconds * 1000).toISOString();
    await h.emit([h.event('Fixture agent reading the exact source.', 'authoring_account', { occurred_at: time(0) })]);
    if (early) {
      await h.emit([h.event('Fixture execution reached its 900-second limit while checking the draft.', 'authoring_account', {
        occurred_at: time(900), detail: detail({ kind: 'preparation', state: 'failed', source: 'harness' }),
      })]);
      await stage(h, 'research', 'failed');
      assert.equal(await h.page.locator('.activity-stage[data-stage="research"] .stage-duration').innerText(), '15m 0s');
    }
    await h.emit([h.event('Capturing completed output and evidence.', 'collecting_output', {
      occurred_at: time(900), detail: detail({ kind: 'preparation', source: 'worker' }),
    })]);
    await stage(h, 'collection');
    await h.page.getByText('Preserving available output and evidence.', { exact: true }).waitFor();
    if (early) assert.equal(await h.page.locator('.activity-stage[data-stage="research"]').getAttribute('data-state'), 'failed');
    await screenshot(h, 'preserving-after-failure');
    await h.emit([h.event('Restoring the saved execution result and evidence.', 'collecting_output', {
      occurred_at: time(934), detail: detail({ kind: 'preparation', source: 'worker' }),
    }), h.event('Agent execution stopped before a completed output was available.', 'authoring_account', {
      occurred_at: time(955), detail: detail({ kind: 'preparation', state: 'failed', source: 'worker' }),
    })]);
    h.state.job.failure = { code: 'AGENT_FAILED', message: 'Fixture: the agent reached its execution limit while checking the draft. No output was accepted.', retryable: true };
    h.state.job.completed_at = time(959);
    await h.emit([h.event(h.state.job.failure.message, 'authoring_account', { occurred_at: time(959), status: 'failed', event_type: 'failure', detail: null })]);
    await h.page.evaluate(() => window.__activityFixture.finish());
    await stage(h, 'outcome', 'failed');
    const research = h.page.locator('.activity-stage[data-stage="research"]');
    const collection = h.page.locator('.activity-stage[data-stage="collection"]');
    const outcome = h.page.locator('.activity-stage[data-stage="outcome"]');
    assert.equal(await research.count(), 1, 'No second timed research stage for a late failure notice');
    assert.equal(await research.getAttribute('data-state'), 'failed');
    assert.equal(await research.locator('.stage-duration').innerText(), '15m 0s');
    assert.equal(await collection.locator('.stage-state').innerText(), 'Preserved');
    assert.equal(await collection.locator('.stage-duration').innerText(), '55s');
    assert.equal(await outcome.locator('.stage-duration, .step-duration').count(), 0, 'Outcome reporting must not look like a four-second agent run');
    assert.equal(await h.page.getByText('Scientific accounts ready', { exact: true }).count(), 0);
    assert.equal(await h.page.getByText('Collecting results', { exact: true }).count(), 0);
    assert.deepEqual(await h.page.locator('.activity-stage').evaluateAll(nodes => nodes.map(node => node.dataset.stage)), ['research', 'collection', 'outcome']);
    await screenshot(h, 'failed-outcome');
    h.result.checks.push('Failed authoring stays failed during preservation; preserved evidence never implies accepted science');
    h.result.checks.push('Historical late failure retains chronology and original agent duration, with an untimed outcome notice');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
    assert.ok(h.state.requests.every(request => request.method === 'GET'));
  } finally { await h.close(); }
}
async function compactToolsScenario(name, viewport) {
  const h = await harness(name, viewport);
  const warning = 'Fixture warning: the selected graph returned no matching rows.';
  const recordName = `039ee${'0123456789abcdef'.repeat(3)}01234569189.json`;
  const fullPath = `input/evidence-records/${recordName}`;
  const rawArguments = JSON.stringify({ file_path: fullPath, offset: 1, limit: 140, note: 'FULL_ARGUMENT_MARKER' });
  const readOutput = JSON.stringify({ format: 'reveal.evidence-record/1', pointer: '/pigean/candidates/gene:fixture',
    value: { note: 'FULL_RECORDED_RESULT_MARKER', identifier: 'fixture_'.repeat(80), observation: 0.123456789 } }).slice(0, -1)
    + ',"recorded_integer":9007199254740993,"recorded_decimal":0.123456789012345678901}';
  const queryArguments = JSON.stringify({ graph: 'prokn', query: 'MATCH (n) WHERE n.id = $id RETURN n LIMIT 10',
    parameters: { id: 'fixture:EXACT_IDENTIFIER' } });
  const queryOutput = JSON.stringify({ row_count: 0, rows: [], status: 'empty', note: 'EMPTY_RESULT_IS_NOT_BIOLOGICAL_ABSENCE' });
  h.state.job.warnings = [warning, warning, warning];
  h.state.gapGate.release();
  try {
    await h.open();
    await h.page.getByText(warning, { exact: false }).first().waitFor();
    const pair = (tool_name, call_id, args, output) => [
      h.event(`Fixture calling ${tool_name}`, 'authoring_account', { detail: detail({ kind: 'tool_call', tool_name, call_id, display_arguments: args }) }),
      h.event(`Fixture ${tool_name} returned`, 'authoring_account', { detail: detail({ kind: 'tool_result', state: 'completed', tool_name, call_id, duration_ms: 234, output_excerpt: output }) }),
    ];
    await h.emit([
      ...pair('Read', 'fixture-call-read-exact', rawArguments, readOutput),
      ...pair('mcp__reveal__query_graph', 'fixture-call-query-exact', queryArguments, queryOutput),
      ...pair('mcp__reveal__get_schema', 'fixture-call-schema-exact', '{"graph":"prokn"}', '{"types":["FixtureNode"]}'),
    ]);
    await stage(h, 'research'); await atTail(h);
    const tools = h.page.locator('.activity-tool'); await until(async () => await tools.count() === 3, 'three paired tool invocations');
    const read = tools.nth(0); const query = tools.nth(1); const schema = tools.nth(2);
    for (const tool of [read, query, schema]) {
      assert.equal(await tool.evaluate(el => el.tagName), 'DETAILS');
      assert.equal(await tool.getAttribute('open'), null, 'Each invocation starts collapsed');
      assert.equal(await tool.locator('.tool-details-body').isVisible(), false);
    }
    const readSummary = await read.locator('.tool-invocation').innerText();
    assert.match(readSummary, /^Read\(/); assert.match(readSummary, /filename\s*=/);
    assert.ok(readSummary.includes('input/') && readSummary.includes('039ee') && readSummary.includes('9189.json'));
    assert.ok(readSummary.includes('…'), 'Long path is shortened in summary');
    assert.ok(!readSummary.includes(fullPath) && !readSummary.includes(recordName));
    assert.ok(!readSummary.includes('FULL_ARGUMENT_MARKER'), 'Summary bounds the number of visible arguments');
    assert.match(await query.locator('.tool-invocation').innerText(), /^QueryGraph\(/);
    assert.match(await schema.locator('.tool-invocation').innerText(), /^GetGraphSchema\(/);
    assert.equal(await query.locator(':scope > summary').getByText('mcp__reveal__query_graph', { exact: false }).count(), 0);
    const collapsedText = await history(h).innerText();
    assert.ok(!collapsedText.includes('FULL_RECORDED_RESULT_MARKER') && !collapsedText.includes('EMPTY_RESULT_IS_NOT_BIOLOGICAL_ABSENCE'));
    assert.ok((await metrics(h)).pageWidth <= viewport.width + 1, 'Compact tools fit the viewport');
    h.result.checks.push('Read has a compact shortened filename and bounded argument summary; MCP calls have friendly names; full results start hidden');
    await screenshot(h, 'collapsed');
    await read.locator(':scope > summary').click();
    const readBody = read.locator('.tool-details-body'); await readBody.waitFor();
    const expandedText = await readBody.innerText();
    for (const exact of [fullPath, 'FULL_ARGUMENT_MARKER', 'FULL_RECORDED_RESULT_MARKER', 'fixture-call-read-exact', 'Recorded arguments', 'Result excerpt', '9007199254740993', '0.123456789012345678901'])
      assert.ok(expandedText.includes(exact), `Expanded invocation preserves ${exact}`);
    assert.equal(JSON.parse(await readBody.locator('pre').last().innerText()).value.observation, 0.123456789);
    const jump = h.page.getByRole('button', { name: 'Jump to latest', exact: true }); await jump.waitFor();
    const paused = await metrics(h);
    await h.emit([h.event('Fixture append during tool inspection: retain the reading position.')]);
    await h.page.getByText('Fixture append during tool inspection: retain the reading position.', { exact: true }).waitFor({ state: 'attached' });
    assert.ok(Math.abs((await metrics(h)).top - paused.top) < 3, 'Tool inspection pauses tail following');
    assert.ok((await metrics(h)).pageWidth <= viewport.width + 1, 'Full recorded arguments and result wrap without horizontal overflow');
    await screenshot(h, 'expanded-read');
    await read.locator(':scope > summary').click();
    await query.locator(':scope > summary').click();
    const queryBody = query.locator('.tool-details-body'); await queryBody.waitFor();
    const queryText = await queryBody.innerText();
    for (const exact of ['mcp__reveal__query_graph', 'fixture-call-query-exact', 'fixture:EXACT_IDENTIFIER', 'MATCH (n) WHERE n.id = $id RETURN n LIMIT 10'])
      assert.ok(queryText.includes(exact), `Expanded MCP invocation preserves ${exact}`);
    assert.deepEqual(JSON.parse(await queryBody.locator('pre').last().innerText()), JSON.parse(queryOutput));
    await screenshot(h, 'expanded-query');
    await jump.click(); await atTail(h);
    await h.emit([h.event('Fixture append after Jump to latest: following resumes.')]); await atTail(h);
    h.result.checks.push('Expanded tools preserve full recorded arguments, result data, raw tool name and call ID; inspection pauses following and Jump to latest resumes');
    const visibleWarnings = h.page.locator('.activity-content .notice').filter({ hasText: warning });
    assert.equal(await visibleWarnings.count(), 1, 'Repeated identical warnings share one card');
    assert.match(await visibleWarnings.innerText(), /3/, 'Grouped warning includes its occurrence count');
    assert.deepEqual(h.state.duplicateKeyWarnings, []); assert.deepEqual(h.state.consoleErrors, []);
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
    assert.ok(h.state.requests.every(request => request.method === 'GET'));
    h.result.checks.push('Three identical saved warnings render one counted card without duplicate-key console warnings');
  } finally { await h.close(); }
}
async function timingAndRefreshScenario(name, viewport, gapLink = false) {
  const h = await harness(name, viewport);
  try {
    h.state.sessionGate = gate(); h.state.jobGate = gate();
    await h.page.addInitScript(() => sessionStorage.removeItem('reveal:composer'));
    await h.page.goto(gapLink ? `${origin}/?gap=${encodeURIComponent(gap.object.id)}&job=${jobId}` : `${origin}/?draft=${draftId}&job=${jobId}`);
    await h.page.getByText('Retrieving job status', { exact: true }).waitFor();
    assert.equal(await h.page.locator('#gap-search').count(), 0);
    assert.equal(await h.page.getByText('Live activity', { exact: true }).count(), 0);
    const bars = await h.page.locator('.loading-surface-pulse i').first().evaluate(el => ({width: el.offsetWidth, height: el.offsetHeight}));
    assert.ok(bars.height > bars.width * 2, 'Status loader uses vertical bars');
    await screenshot(h, 'retrieving');
    h.state.sessionGate.release();
    await until(() => h.state.requests.some(r => r.path === `/api/backend/v1/jobs/${jobId}`), 'job requested after session');
    assert.ok(await h.page.getByText('Retrieving job status', { exact: true }).isVisible());
    h.state.jobGate.release();
    await h.page.getByRole('region', {name: 'Research activity', exact: true}).waitFor();
    await h.page.getByText('Retrieving job status and activity…', {exact:true}).waitFor();
    assert.equal(await h.page.locator('.trending').count(), 0);
    assert.equal(await h.page.locator('.stage-duration').innerText(), '—');
    h.result.checks.push('Immediate status before session/job response; live activity opens independently of delayed gap; unknown timing is not fabricated');
    const started = Date.now() - 5000;
    const time = offset => new Date(started + offset).toISOString();
    await h.emit([h.event('Queued for evidence preparation.', 'preparing_evidence', {occurred_at:time(0), detail:detail({kind:'preparation',source:'worker'})})]);
    const timer = h.page.locator('.activity-stage[data-state="working"] .stage-duration');
    const before = await timer.innerText();
    await until(async () => (await timer.innerText()) !== before, 'live stage timer ticks');
    assert.equal(await h.page.locator('.system-update[data-step-state="working"] .work-dot.is-working').count(), 1);
    await h.emit([h.event('Collecting selected source evidence.', 'preparing_evidence', {occurred_at:time(2000), detail:detail({kind:'preparation',source:'worker'})})]);
    assert.equal(await h.page.locator('.system-update[data-step-state="completed"] .work-dot.is-completed').count(), 1);
    assert.equal(await h.page.locator('.system-update[data-step-state="working"] .work-dot.is-working').count(), 1);
    assert.equal(await h.page.locator('.system-update[data-step-state="completed"] .step-duration').innerText(), '2s');
    const workDot = await h.page.locator('.work-dot.is-working').evaluate(el => ({animation:getComputedStyle(el).animationName,color:getComputedStyle(el).backgroundColor}));
    const doneDot = await h.page.locator('.work-dot.is-completed').evaluate(el => ({animation:getComputedStyle(el).animationName,color:getComputedStyle(el).backgroundColor}));
    assert.notEqual(workDot.animation, 'none'); assert.equal(doneDot.animation, 'none'); assert.notEqual(workDot.color, doneDot.color);
    await screenshot(h, 'working');
    await h.emit([h.event('Preparing the isolated runtime.', 'starting_agent', {occurred_at:time(4000), detail:detail({kind:'preparation',source:'worker'})})]);
    assert.equal(await h.page.locator('.activity-stage[data-stage="preparation"] .stage-duration').innerText(), '4s');
    await h.emit([h.event('Fixture timeout: this attempt could not complete.', 'starting_agent', {occurred_at:time(6000), status:'failed', detail:detail({kind:'preparation',source:'worker'})})]);
    await stage(h, 'setup', 'failed');
    const stopped = await h.page.locator('.stage-duration').allTextContents();
    await new Promise(resolve => setTimeout(resolve, 1100));
    assert.deepEqual(await h.page.locator('.stage-duration').allTextContents(), stopped);
    assert.equal(await h.page.locator('.work-dot.is-working').count(), 0);
    assert.ok((await metrics(h)).pageWidth <= viewport.width + 1);
    await h.page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await h.page.locator('.loading-surface-pulse i').first().evaluate(el => getComputedStyle(el).animationName), 'none');
    h.state.gapGate.release();
    await h.page.locator('.selected-question').waitFor();
    assert.equal(await h.page.getByRole('region', {name: 'Research activity', exact: true}).count(), 1);
    h.result.checks.push('Delayed gap restoration retains the explicit job and does not submit suggestions or draft writes');
    h.result.checks.push('Amber heartbeat changes to static green, live timers tick, completed/failed timings freeze, mobile layout fits, reduced motion respected');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []); assert.deepEqual(h.state.duplicateKeyWarnings, []);
  } finally { await h.close(); }
}
try {
  const filter = process.env.ACTIVITY_SCENARIO_FILTER ? new RegExp(process.env.ACTIVITY_SCENARIO_FILTER) : null;
  const scenarios = [
    ['timing-refresh-desktop', () => timingAndRefreshScenario('timing-refresh-desktop', {width:1280,height:900})],
    ['timing-refresh-mobile', () => timingAndRefreshScenario('timing-refresh-mobile', {width:390,height:844}, true)],
    ['desktop', () => activityScenario('desktop', { width: 1280, height: 900 })],
    ['mobile', () => activityScenario('mobile', { width: 390, height: 844 })],
    ['replay-ahead', replayAheadScenario],
    ['output-collection-desktop', () => outputCollectionScenario('output-collection-desktop', { width: 1280, height: 900 })],
    ['output-collection-mobile', () => outputCollectionScenario('output-collection-mobile', { width: 390, height: 844 })],
    ['failed-capture-historical', () => failedCaptureScenario('failed-capture-historical', { width: 1280, height: 900 }, false)],
    ['failed-capture-early-mobile', () => failedCaptureScenario('failed-capture-early-mobile', { width: 390, height: 844 }, true)],
    ['compact-tools-desktop', () => compactToolsScenario('compact-tools-desktop', { width: 1280, height: 900 })],
    ['compact-tools-mobile', () => compactToolsScenario('compact-tools-mobile', { width: 390, height: 844 })],
  ].filter(([name]) => !filter || filter.test(name));
  assert.ok(scenarios.length, 'Scenario filter must select a test');
  for (const [, run] of scenarios) await run();
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.failure = error.stack || error.message; process.exitCode = 1;
} finally {
  await browser.close();
  await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
