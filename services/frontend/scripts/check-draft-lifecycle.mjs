#!/usr/bin/env node
/** Stateful, mocked local browser regression. No API request reaches a backend.
 * DRAFT_LIFECYCLE_BASE_URL, DRAFT_LIFECYCLE_AUDIT_DIR and
 * DRAFT_LIFECYCLE_SCENARIO_FILTER may override the local defaults. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.DRAFT_LIFECYCLE_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Use a local frontend');
const output = resolve(process.env.DRAFT_LIFECYCLE_AUDIT_DIR || resolve(root, '.runtime/draft-lifecycle-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0], factor = fixture.suggestions.automatic_anchors[0].factor;
const user = '11111111-1111-4111-8111-111111111111', stamp = '2026-10-01T12:00:00Z';
const clone = value => structuredClone(value);
const pageOf = items => ({ items: clone(items), page: { next_cursor: null, has_more: false, snapshot_id: 'local-fixture' } });
const selectedGap = { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision };
const savedComposer = () => ({ source_gap: selectedGap, eaggl_anchors: [{ reference: { source: 'eaggl', source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }], dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: [], research_direction: 'The explicitly saved direction', context: 'The explicitly saved context', hypotheses: 'The explicitly saved working hypothesis', upload_ids: [] });
const uuid = value => `22222222-2222-4222-8222-${String(value).padStart(12, '0')}`;
const problem = (route, status, code, detail) => route.fulfill({ status, json: { code, detail } });
async function playwright() { if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE); try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); } }
async function executable() { if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH; for (const name of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(value => /^chromium-\d+$/.test(value)).sort().reverse()) { const path = resolve(homedir(), 'Library/Caches/ms-playwright', name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'); try { await access(path); return path; } catch {} } }
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
const activeHarnesses = [];
const report = { scope: 'Stateful intercepted APIs only. Real frontend editor, dialogs, browser navigation and upload XHR; no real authentication, database, S3 write or agent dispatch.', status: 'running', scenarios: [] };
async function until(check, description) { const start = Date.now(); while (!await check()) { assert.ok(Date.now() - start < 15000, `Timed out: ${description}`); await new Promise(resolve => setTimeout(resolve, 30)); } }

async function harness(name, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 950 }, serviceWorkers: 'block', reducedMotion: 'reduce' });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const state = { signed: !options.anonymous, drafts: new Map(), jobs: new Map(), frozen: new Map(), uploads: new Map(), receipts: new Map(), calls: [], errors: [], unexpected: [], sequence: 10, lostJob: false, lostSave: false, lostUpload: false, transfers: 0, holds: new Set(options.hold ? [options.hold] : []), registered: false, failures: new Set(), gates: new Map() };
  const hold = async phase => { if (!state.holds.has(phase)) return; if (!state.gates.has(phase)) { let release; const promise = new Promise(resolve => { release = resolve; }); state.gates.set(phase, { promise, release }); } await state.gates.get(phase).promise; };
  const release = phase => { state.holds.delete(phase); state.gates.get(phase)?.release(); };
  let releaseSuggestions;
  const suggestionsReady = new Promise(resolve => { releaseSuggestions = resolve; });
  if (!options.holdSuggestions) releaseSuggestions();
  if (options.saved) state.drafts.set(uuid(1), { id: uuid(1), owner_user_id: user, name: 'Named research draft', lifecycle: 'saved', version: 1, composer: savedComposer(), created_at: stamp, updated_at: stamp, expires_at: null });
  page.on('pageerror', error => state.errors.push(error.message));
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname), method = request.method();
    if (url.origin !== origin) { state.unexpected.push(`${method} ${url.href}`); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    const body = request.postData() ? (request.headers()['content-type']?.includes('application/x-www-form-urlencoded') ? Object.fromEntries(new URLSearchParams(request.postData())) : request.postDataJSON()) : undefined, key = request.headers()['idempotency-key'];
    state.calls.push({ method, path, key, body: clone(body) });
    const respond = (value, status = 200) => route.fulfill({ status, json: clone(value) }).catch(error => { if (!/closed|cancel|handled/i.test(error.message)) throw error; });
    const phase = path === '/api/session/status' ? 'status' : path === '/api/session/anonymous' ? 'anonymous' : path === '/api/backend/v1/me' ? 'me' : path === '/api/backend/v1/drafts' && method === 'POST' ? 'draft' : path === '/api/backend/v1/jobs' && method === 'POST' ? 'job' : path === '/api/auth/csrf' ? 'csrf' : path.startsWith('/api/auth/signin/') ? 'oauth' : path === `/api/backend/v1/drafts/${uuid(1)}` && method === 'GET' ? 'restore' : null;
    if (phase) await hold(phase);
    if (options.fail === phase && !state.failures.has(phase)) { state.failures.add(phase); return problem(route, 503, 'FIXTURE_OUTAGE', `Simulated ${phase} outage. Please retry.`); }
    const receiptKey = `${method}:${path}:${key}`;
    const receipt = () => state.receipts.get(receiptKey);
    const remember = value => { assert.ok(key, `Missing idempotency key for ${path}`); state.receipts.set(receiptKey, clone(value)); return value; };
    if (path === '/api/session/status') return respond({ principal: state.signed ? { user_id: user } : null, canClaim: false, canAdmin: false, providers: { google: true, orcid: true } });
    if (path === '/api/auth/csrf') return respond({ csrfToken: 'intercepted-lifecycle-token' });
    if (path === '/api/auth/session') return respond({});
    if (path.startsWith('/api/auth/signin/') && method === 'POST') { state.signed = true; state.registered = true; return respond({ url: `${origin}/?oauth-fixture-return=1` }); }
    if (path === '/api/session/anonymous' && method === 'POST') { state.signed = true; return respond({ user_id: user }); }
    if (path === '/api/backend/v1/me') return state.signed ? respond({ user_id: user, principal_kind: state.registered ? 'registered' : 'anonymous', display_name: 'Lifecycle fixture', person: null, email: null, email_verified: false, orcid: null, orcid_authenticated: false, workspace_expires_at: null }) : problem(route, 401, 'SESSION_EXPIRED', 'No session');
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': fixture heartbeat\n\n' });
    if (path === '/api/backend/v1/knowledge-gaps') return respond(pageOf([gap]));
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return state.gapUnavailable ? problem(route, 503, 'CATALOG_UNAVAILABLE', 'Current source catalog is unavailable') : respond(gap);
    if (path.endsWith('/vote')) return respond(gap.votes || { upvotes: 0, downvotes: 0, score: 0, user_vote: null, can_vote: false });
    if (/\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(path)) return respond(pageOf([]));
    if (path === '/api/backend/v1/mechanisms/suggest') { await suggestionsReady; return respond(fixture.suggestions); }
    if (path.startsWith('/api/backend/v1/mechanisms/')) return respond(factor);
    if (path === '/api/backend/v1/me/explorations') return respond(method === 'POST' ? { ...body, knowledge_gap: gap.object, last_explored_at: stamp, draft_id: null } : pageOf([]));
    if (path === '/api/backend/v1/drafts' && method === 'GET') return respond(pageOf([...state.drafts.values()].filter(value => value.lifecycle === 'saved')));
    if (path === '/api/backend/v1/drafts' && method === 'POST') {
      if (receipt()) return respond(receipt(), 201);
      assert.ok(state.signed, 'Provision session before creating an editor');
      if (body.source_draft_id && state.drafts.get(body.source_draft_id)?.version !== body.source_draft_version) return problem(route, 409, 'VERSION_CONFLICT', 'The source draft changed in another session.');
      const value = { id: uuid(state.sequence++), owner_user_id: user, version: 1, lifecycle: body.lifecycle || 'saved', composer: body.composer, name: body.name, source_draft_id: body.source_draft_id, source_draft_version: body.source_draft_version, created_at: stamp, updated_at: stamp, expires_at: body.lifecycle === 'temporary' ? '2026-10-02T12:00:00Z' : null };
      state.drafts.set(value.id, clone(value)); return respond(remember(value), 201);
    }
    const draftMatch = /^\/api\/backend\/v1\/drafts\/([^/]+)$/.exec(path);
    if (draftMatch) {
      if (method !== 'GET' && receipt()) return respond(receipt());
      const value = state.drafts.get(draftMatch[1]);
      if (!value) return problem(route, 404, 'NOT_FOUND', 'This draft is unavailable');
      if (method === 'GET') return respond(value);
      if (body.expected_version !== value.version) return problem(route, 409, 'VERSION_CONFLICT', 'This draft changed in another tab.');
      if (method === 'DELETE') { state.drafts.delete(value.id); return respond(remember({ id: value.id, deleted: true })); }
      if (method === 'PATCH') {
        Object.assign(value, body, { version: value.version + 1, updated_at: stamp }); delete value.expected_version;
        remember(value);
        if (options.loseSave && !state.lostSave) { state.lostSave = true; return route.abort('failed'); }
        return respond(value);
      }
    }
    if (path === '/api/backend/v1/jobs' && method === 'POST') {
      if (receipt()) return respond(receipt(), 202);
      const input = state.drafts.get(body.draft_id); assert.ok(input); assert.equal(input.version, body.draft_version);
      const id = uuid(state.sequence++), requestId = uuid(state.sequence++);
      const job = { id, owner_user_id: user, kind: 'analysis', status: 'failed', stage: 'complete', research_request_id: requestId, input_account_id: null, created_at: stamp, updated_at: stamp, completed_at: stamp, result: null, failure: { code: 'FIXTURE_ONLY', message: 'Mock run: no agent was dispatched.', retryable: false }, warnings: [], last_event_id: '0', links: { self: `/v1/jobs/${id}`, events: `/v1/jobs/${id}/events` } };
      state.jobs.set(id, clone(job)); state.frozen.set(requestId, { id: requestId, owner_user_id: user, source_draft_id: input.source_draft_id || input.id, source_draft_version: input.version, composer: clone(input.composer), question_id: gap.object.id, document: { knowledge_gaps: [clone(gap.object)] }, created_at: stamp }); remember(job);
      if (options.loseJob && !state.lostJob) { state.lostJob = true; return route.abort('failed'); }
      return respond(job, 202);
    }
    if (path === '/api/backend/v1/jobs') return respond(pageOf([...state.jobs.values()]));
    const jobMatch = /^\/api\/backend\/v1\/jobs\/([^/]+)(\/events)?$/.exec(path);
    if (jobMatch) return jobMatch[2] ? route.fulfill({ contentType: 'text/event-stream', body: '' }) : respond(state.jobs.get(jobMatch[1]));
    if (path.startsWith('/api/backend/v1/research-requests/')) return respond(state.frozen.get(path.split('/').at(-1)));
    if (path === '/api/backend/v1/research-requests') return respond(pageOf([...state.frozen.values()]));
    if (path === '/api/backend/v1/accounts' || path === '/api/backend/v1/analysis-outcomes') return respond(pageOf([]));
    if (path === '/api/backend/v1/uploads' && method === 'POST') {
      if (receipt()) return respond(receipt(), 201);
      if ([...state.uploads.values()].filter(value => value.draft_id === body.draft_id && value.status !== 'removed').length >= 5) return problem(route, 422, 'UPLOAD_LIMIT', 'An editor accepts at most five files.');
      const id = uuid(state.sequence++), upload = { id, ...body, media_type: 'text/plain', status: 'pending', created_at: stamp, expires_at: '2026-10-02T12:00:00Z', storage: null, extraction: null, error: null };
      state.uploads.set(id, clone(upload)); return respond(remember({ upload, transfer: { method: 'POST', url: `/v1/uploads/${id}/content`, fields: {}, encoding: 'base64' } }), 201);
    }
    const uploadMatch = /^\/api\/backend\/v1\/uploads\/([^/]+)(?:\/(content|complete|transfer))?$/.exec(path);
    if (uploadMatch) {
      const value = state.uploads.get(uploadMatch[1]); assert.ok(value, 'Upload exists');
      if (uploadMatch[2] === 'content') {
        if (value.status !== 'pending') return problem(route, 409, 'UPLOAD_EXPIRED', 'Start a new upload.');
        const bytes = Buffer.from(body.content_base64, 'base64'); assert.equal(bytes.length, value.size_bytes); assert.equal(createHash('sha256').update(bytes).digest('hex'), value.sha256); state.transfers++; value.staged = true; return respond(value);
      }
      if (uploadMatch[2] === 'complete') {
        assert.ok(value.staged || value.status === 'ready');
        value.status = 'ready'; value.storage = { store: 's3', bucket: 'local-fixture', key: `uploads/${value.id}`, version_id: 'fixture-version', sha256: value.sha256, size_bytes: value.size_bytes, content_type: 'text/plain' };
        if (options.loseUpload && !state.lostUpload) { state.lostUpload = true; return route.abort('failed'); }
        return respond(value);
      }
      if (method === 'DELETE') { value.status = 'removed'; return respond(value); }
      return respond(value);
    }
    state.unexpected.push(`${method} ${path}`); return problem(route, 500, 'UNEXPECTED_FIXTURE_ROUTE', 'Unmocked API route blocked');
  });
  const h = { page, state, releaseSuggestions, release,
    async seedPending(method = 'session') { await page.addInitScript(attempt => { if (!sessionStorage.getItem('lifecycle-receipt-seeded')) { sessionStorage.setItem('reveal:submission', JSON.stringify(attempt)); sessionStorage.setItem('lifecycle-receipt-seeded', '1'); } }, { method, question: gap.object.text, gap, composer: savedComposer(), draft: state.drafts.get(uuid(1)) || null, owner: state.signed ? user : null, anonymousKey: uuid(6), requestKeys: [], submitKey: null }); },
    async openContext() {
      const disclosure = page.locator('details.additional-context'); await disclosure.waitFor();
      if (await disclosure.getAttribute('open') === null) await disclosure.locator(':scope > summary').click();
      await page.locator('#research-context').waitFor();
    },
    async openLegacy() {
      await h.openContext();
      const disclosure = page.locator('details.previous-research-inputs'); await disclosure.waitFor();
      if (await disclosure.getAttribute('open') === null) await disclosure.locator(':scope > summary').click();
      await page.locator('#research-direction').waitFor(); await page.locator('#research-hypotheses').waitFor();
      assert.equal(await page.locator('#research-direction').getAttribute('maxlength'), '6000');
      assert.equal(await page.locator('#research-hypotheses').getAttribute('maxlength'), '12000');
    },
    async openNew() {
      await page.goto(`${origin}/?gap=${encodeURIComponent(gap.object.id)}`); await page.waitForURL(/\/drafts\//);
      const disclosure = page.locator('details.additional-context'); await disclosure.waitFor();
      assert.equal(await disclosure.getAttribute('open'), null, 'Additional context starts collapsed');
      await h.openContext();
      assert.equal(await page.locator('#research-context').getAttribute('maxlength'), '20000');
      assert.equal(await page.locator('#research-direction, #research-hypotheses').count(), 0, 'New editors expose one context field');
    },
    async openSaved() { await page.goto(`${origin}/drafts/${uuid(1)}`); await h.openLegacy(); },
    async readyToSubmit() { await until(() => page.getByRole('button', { name: 'Let’s close this gap', exact: true }).isEnabled(), 'submission becomes available'); },
    async close(checks) { assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []); await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true }); report.scenarios.push({ name, status: 'passed', checks, calls: state.calls }); await context.close(); },
  };
  activeHarnesses.push(h); return h;
}

const scenarios = [
  ['anonymous-editor-before-suggestions', async () => {
    const h = await harness('anonymous-editor-before-suggestions', { anonymous: true, holdSuggestions: true }); await h.openNew();
    const paths = h.state.calls.map(call => call.path);
    assert.ok(paths.indexOf('/api/session/anonymous') < paths.indexOf('/api/backend/v1/drafts'));
    assert.equal(h.state.drafts.size, 1); assert.equal([...h.state.drafts.values()][0].lifecycle, 'temporary');
    await h.page.locator('#research-context').fill('Typing while suggestions are still loading'); h.releaseSuggestions(); await h.readyToSubmit();
    assert.equal(await h.page.locator('#research-context').inputValue(), 'Typing while suggestions are still loading');
    assert.equal(h.state.jobs.size, 0); await h.close(['anonymous principal before editor', 'URL and editor available before suggestions', 'typed context retained']);
  }],
  ['abandon-unsaved-without-retaining-draft', async () => {
    const h = await harness('abandon-unsaved-without-retaining-draft'); await h.openNew(); await h.readyToSubmit();
    await h.page.locator('#research-context').fill('Discard this working context'); await h.page.waitForTimeout(1300);
    assert.equal(h.state.calls.filter(call => call.method === 'PATCH').length, 0);
    assert.equal([...h.state.drafts.values()][0].composer.context || '', '');
    await h.page.getByRole('button', { name: 'Search for a different knowledge gap', exact: true }).click();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    await until(() => h.state.drafts.size === 0, 'temporary draft discarded');
    await h.page.reload(); await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    assert.equal(await h.page.locator('#research-context').count(), 0); assert.equal(h.state.jobs.size, 0);
    await h.close(['no autosave after edit', 'deliberate discard', 'reload stays at discovery']);
  }],
  ['cancel-name-and-explicit-save', async () => {
    const h = await harness('cancel-name-and-explicit-save'); await h.openNew(); await h.readyToSubmit(); await h.page.locator('#research-context').fill('A named hypothesis investigation');
    await h.page.getByRole('button', { name: 'Save draft', exact: true }).click();
    let dialog = h.page.getByRole('dialog', { name: 'Save your draft' }); await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal([...h.state.drafts.values()][0].lifecycle, 'temporary'); assert.equal(h.state.calls.filter(call => call.method === 'PATCH').length, 0);
    await h.page.getByRole('button', { name: 'Save draft', exact: true }).click(); dialog = h.page.getByRole('dialog', { name: 'Save your draft' }); await dialog.getByLabel('Draft name').fill('My explicitly saved draft'); await dialog.getByRole('button', { name: 'Save draft', exact: true }).click();
    await until(() => [...h.state.drafts.values()][0]?.lifecycle === 'saved', 'explicit save is committed'); await dialog.waitFor({ state: 'hidden' });
    const saved = clone([...h.state.drafts.values()][0]); assert.equal(saved.name, 'My explicitly saved draft'); assert.equal(saved.composer.context, 'A named hypothesis investigation');
    assert.equal(saved.composer.research_direction || '', ''); assert.equal(saved.composer.hypotheses || '', '');
    await h.page.locator('#research-context').fill('This newer edit must be discarded'); await h.page.waitForTimeout(1300); await h.page.reload(); await h.openContext();
    assert.equal(await h.page.locator('#research-context').inputValue(), saved.composer.context); assert.equal(h.state.drafts.size, 1); assert.equal(h.state.jobs.size, 0);
    await h.close(['cancel retains no named draft', 'explicit Save stores name and content', 'reload restores last saved revision']);
  }],
  ['temporary-reload-does-not-resurrect-inputs', async () => {
    const h = await harness('temporary-reload-does-not-resurrect-inputs'); await h.openNew(); await h.readyToSubmit(); const oldId = [...h.state.drafts.keys()][0];
    await h.page.locator('#research-context').fill('Do not restore this unsaved text'); await h.page.reload(); await h.openContext();
    await until(() => h.state.drafts.size === 1 && !h.state.drafts.has(oldId), 'new empty working copy replaces old one');
    assert.equal(await h.page.locator('#research-context').inputValue(), ''); assert.ok([...h.state.drafts.values()].every(value => value.lifecycle === 'temporary'));
    await h.close(['unsaved reload drops text', 'gap retained in fresh temporary editor']);
  }],
  ['named-submit-independent-frozen-run', async () => {
    const h = await harness('named-submit-independent-frozen-run', { saved: true }); await h.openSaved(); await h.readyToSubmit();
    const saved = clone(h.state.drafts.get(uuid(1)));
    assert.equal(await h.page.locator('#research-direction').inputValue(), saved.composer.research_direction);
    assert.equal(await h.page.locator('#research-hypotheses').inputValue(), saved.composer.hypotheses);
    await h.page.locator('#research-direction').fill(''); await h.page.locator('#research-hypotheses').fill('');
    assert.equal(await h.page.locator('#research-direction').isVisible(), true, 'Clearing legacy text keeps its control editable');
    assert.equal(await h.page.locator('#research-hypotheses').isVisible(), true, 'Clearing the last legacy field keeps both controls editable');
    await h.page.locator('#research-direction').fill('Direction submitted for this run only');
    await h.page.locator('#research-hypotheses').fill(saved.composer.hypotheses);
    await h.page.locator('#research-context').fill('Frozen run-specific context');
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.waitForURL(/\/runs\//); await h.page.getByRole('region', { name: 'Submitted research inputs', exact: true }).waitFor();
    assert.deepEqual(h.state.drafts.get(uuid(1)), saved);
    const create = h.state.calls.find(call => call.method === 'POST' && call.path === '/api/backend/v1/drafts'); assert.equal(create.body.lifecycle, 'temporary'); assert.equal(create.body.source_draft_id, uuid(1)); assert.equal(create.body.source_draft_version, saved.version);
    const frozen = [...h.state.frozen.values()][0].composer;
    assert.equal(frozen.research_direction, 'Direction submitted for this run only');
    assert.equal(frozen.context, 'Frozen run-specific context'); assert.equal(frozen.hypotheses, saved.composer.hypotheses);
    h.state.drafts.delete(uuid(1)); const before = h.state.calls.length; await h.page.reload();
    await h.page.getByRole('region', { name: 'Submitted research inputs', exact: true }).waitFor(); assert.ok(await h.page.getByText('Direction submitted for this run only', { exact: true }).count());
    assert.equal(await h.page.getByText(frozen.context, { exact: true }).count(), 1);
    assert.equal(await h.page.getByText(frozen.hypotheses, { exact: true }).count(), 1);
    assert.equal(h.state.calls.slice(before).some(call => call.path === `/api/backend/v1/drafts/${uuid(1)}`), false);
    assert.equal(h.state.jobs.size, 1); await h.close(['legacy controls remain editable after clearing', 'all three fields retain independent semantics', 'named draft unchanged', 'independent temporary submission snapshot', 'frozen run survives source deletion']);
  }],
  ['lost-submission-retries-one-key-after-reload', async () => {
    const h = await harness('lost-submission-retries-one-key-after-reload', { saved: true, loseJob: true }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.locator('[data-submission-state="error"]').waitFor();
    assert.equal(h.state.jobs.size, 1); const before = h.state.calls.find(call => call.method === 'POST' && call.path === '/api/backend/v1/jobs');
    await h.page.reload(); await h.page.waitForURL(/\/runs\//); await h.page.getByRole('region', { name: 'Submitted research inputs', exact: true }).waitFor();
    const attempts = h.state.calls.filter(call => call.method === 'POST' && call.path === '/api/backend/v1/jobs'); assert.ok(attempts.length >= 2); assert.ok(attempts.every(call => call.key === before.key && JSON.stringify(call.body) === JSON.stringify(before.body)));
    assert.equal(h.state.jobs.size, 1); assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
    await h.close(['commit survives lost acknowledgment', 'reload resumes exact key/body', 'single run and cleared receipt']);
  }],
  ['frozen-run-survives-catalog-outage', async () => {
    const h = await harness('frozen-run-survives-catalog-outage', { saved: true }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.waitForURL(/\/runs\//);
    h.state.gapUnavailable = true; await h.page.reload(); await h.page.getByRole('region', { name: 'Submitted research inputs' }).waitFor();
    assert.equal(await h.page.locator('.selected-question').innerText(), gap.object.text);
    assert.equal(await h.page.getByText(savedComposer().research_direction, { exact: true }).count(), 1);
    await h.page.getByRole('alert').filter({ hasText: /current knowledge gap could not be loaded/i }).waitFor();
    assert.equal(await h.page.getByRole('button', { name: 'Edit these inputs', exact: true }).count(), 0);
    await h.close(['frozen inputs and question survive current catalog failure', 'editing requires a valid current gap']);
  }],
  ['lost-save-retries-one-key', async () => {
    const h = await harness('lost-save-retries-one-key', { saved: true, loseSave: true }); await h.openSaved(); await h.page.locator('#research-direction').fill('Saved even when acknowledgment is lost');
    await h.page.getByRole('button', { name: 'Save draft', exact: true }).click(); await h.page.getByRole('alert').filter({ hasText: /fetch|network|failed/i }).first().waitFor();
    await h.page.reload(); await h.openLegacy(); await until(() => h.state.calls.filter(call => call.method === 'PATCH').length >= 2, 'same save resumed on reload');
    assert.equal(await h.page.locator('#research-direction').inputValue(), 'Saved even when acknowledgment is lost');
    await until(() => h.page.evaluate(() => sessionStorage.getItem('reveal:explicit-save') === null), 'save receipt cleared');
    const writes = h.state.calls.filter(call => call.method === 'PATCH'); assert.equal(writes[0].key, writes[1].key); assert.deepEqual(writes[0].body, writes[1].body); assert.equal(h.state.drafts.get(uuid(1)).version, 2);
    await h.close(['reload resumes explicit save using its exact receipt', 'one version change', 'save receipt cleared']);
  }],
  ['conflict-cancel-copy-and-load-saved', async () => {
    const h = await harness('conflict-cancel-copy-and-load-saved', { saved: true }); await h.openSaved(); await h.readyToSubmit();
    h.state.drafts.get(uuid(1)).version = 2; h.state.drafts.get(uuid(1)).composer.research_direction = 'The newer saved revision from another tab';
    await h.page.locator('#research-direction').fill('Discard this conflicting local change'); await h.page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await h.page.getByRole('button', { name: 'Save my edits as a new draft', exact: true }).click();
    await h.page.getByRole('dialog', { name: 'Save your draft' }).getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).isEnabled(), true);
    await h.page.getByRole('button', { name: 'Load saved version', exact: true }).click();
    await h.openLegacy();
    await until(() => h.page.locator('#research-direction').inputValue().then(value => value === 'The newer saved revision from another tab'), 'latest saved revision restored');
    const writes = h.state.calls.filter(call => call.method === 'PATCH').length; await h.page.reload(); await h.openLegacy();
    assert.equal(await h.page.locator('#research-direction').inputValue(), 'The newer saved revision from another tab');
    assert.equal(h.state.calls.filter(call => call.method === 'PATCH').length, writes, 'Discarded conflicting Save must not replay on reload');
    assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:explicit-save')), null);
    await h.close(['Cancel copy leaves usable original editor', 'Load saved discards conflicting Save intent', 'reload stays on selected saved revision']);
  }],
  ['upload-completion-retry-and-remove', async () => {
    const h = await harness('upload-completion-retry-and-remove', { loseUpload: true }); await h.openNew(); await h.readyToSubmit();
    await h.page.locator('input[type="file"]').setInputFiles({ name: 'observations.txt', mimeType: 'text/plain', buffer: Buffer.from('Independent source observation\n') });
    await h.page.locator('.attachment-list').getByRole('button', { name: 'Retry', exact: true }).waitFor();
    assert.equal(h.state.transfers, 1); assert.equal(await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).isDisabled(), true);
    await h.page.locator('details.additional-context > summary').click();
    assert.equal(await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).isDisabled(), true, 'Collapsing context cannot clear an unfinished upload');
    await h.openContext();
    await h.page.locator('.attachment-list').getByRole('button', { name: 'Retry', exact: true }).click(); await h.page.getByText(/Ready for the agent/).waitFor(); assert.equal(h.state.transfers, 1, 'Already-completed bytes must not be transferred again');
    await h.page.getByRole('button', { name: 'Remove observations.txt', exact: true }).click(); await until(() => [...h.state.uploads.values()][0].status === 'removed', 'detached upload released');
    for (let i = 0; i < 5; i++) { await h.page.locator('input[type="file"]').setInputFiles({ name: `replacement-${i}.txt`, mimeType: 'text/plain', buffer: Buffer.from(`replacement ${i}`) }); await h.page.getByText(/Ready for the agent/).waitFor(); await h.page.getByRole('button', { name: `Remove replacement-${i}.txt`, exact: true }).click(); await until(() => [...h.state.uploads.values()].every(value => value.status === 'removed'), 'replacement released'); }
    assert.equal(await h.page.locator('.attachment-list li').count(), 0); await h.readyToSubmit();
    await h.close(['unfinished upload blocks submission even while context is collapsed', 'lost completion resumes metadata without duplicate transfer', 'remove releases quota for replacements']);
  }],
  ['uncertain-response-back-locks-inputs', async () => {
    const h = await harness('uncertain-response-back-locks-inputs', { saved: true, loseJob: true }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.locator('[data-submission-state="error"]').waitFor();
    await h.page.getByRole('button', { name: 'Back to question', exact: true }).click();
    await h.openLegacy();
    assert.equal(await h.page.locator('#research-direction').isDisabled(), true);
    assert.equal(await h.page.locator('#research-context').isDisabled(), true);
    assert.equal(await h.page.locator('#research-hypotheses').isDisabled(), true);
    assert.equal(await h.page.getByRole('button', { name: 'Save draft', exact: true }).isDisabled(), true);
    assert.ok(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')));
    await h.page.getByRole('button', { name: 'Check submission', exact: true }).evaluate(button => { button.click(); button.click(); button.click(); });
    await h.page.waitForURL(/\/runs\//); assert.equal(h.state.jobs.size, 1);
    const attempts = h.state.calls.filter(call => call.method === 'POST' && call.path.endsWith('/jobs')); assert.equal(attempts.length, 2); assert.equal(attempts[0].key, attempts[1].key);
    await h.close(['uncertain Back keeps exact receipt', 'inputs locked until acknowledged', 'rapid retry creates one run']);
  }],
  ...['anonymous', 'status', 'me', 'draft', 'job', 'job-body'].map(phase => [`${phase}-deadline-safe-retry`, async () => {
    const name = `${phase}-deadline-safe-retry`;
    const h = await harness(name, { saved: true, anonymous: phase === 'anonymous', hold: phase === 'job-body' ? undefined : phase });
    await h.seedPending(phase === 'anonymous' ? 'anonymous' : 'session');
    if (phase === 'job-body') await h.page.addInitScript(() => {
      const original = window.fetch.bind(window); let stalled = false;
      window.fetch = async (...args) => { const response = await original(...args); const request = args[0]; const url = typeof request === 'string' ? request : request.url;
        if (!stalled && new URL(url, location.href).pathname === '/api/backend/v1/jobs') { stalled = true; response.json = () => { window.__lifecycleBodyStalled = true; return new Promise(() => {}); }; } return response; };
    });
    await h.page.clock.install(); await h.page.goto(origin);
    await until(() => phase === 'job-body' ? h.page.evaluate(() => window.__lifecycleBodyStalled === true) : h.state.gates.has(phase), `${phase} pending`);
    // Status and identity can first time out during Session boot and then again
    // during the pending submission's explicit refresh; both remain bounded.
    await h.page.clock.fastForward(30_100);
    if (['status', 'me'].includes(phase)) { await h.page.waitForTimeout(50); await h.page.clock.fastForward(30_100); }
    const error = h.page.locator('[data-submission-state="error"]'); await error.waitFor({ timeout: 2500 });
    assert.match(await error.getByRole('alert').innerText(), /retry|again|confirm/i); h.release(phase);
    await error.getByRole('button', { name: 'Retry', exact: true }).evaluate(button => { button.click(); button.click(); button.click(); });
    await h.page.waitForURL(/\/runs\//); assert.equal(h.state.jobs.size, 1);
    const path = phase === 'anonymous' ? '/api/session/anonymous' : phase === 'draft' ? '/api/backend/v1/drafts' : phase.startsWith('job') ? '/api/backend/v1/jobs' : null;
    if (path) { const attempts = h.state.calls.filter(call => call.method === 'POST' && call.path === path); assert.equal(attempts.length, 2); assert.ok(attempts[0].key); assert.equal(attempts[0].key, attempts[1].key); assert.deepEqual(attempts[0].body, attempts[1].body); }
    await h.close(['bounded request including body decoding', 'recoverable error', 'rapid retry preserves mutation key and one run']);
  }]),
  ...['google', 'orcid'].flatMap(provider => ['return', 'csrf', 'oauth'].map(phase => [`${provider}-oauth-${phase}-recovery`, async () => {
    const name = `${provider}-oauth-${phase}-recovery`, h = await harness(name, { anonymous: phase !== 'return' });
    h.state.registered = phase === 'return'; await h.seedPending(provider);
    await h.page.goto(origin);
    if (phase !== 'return') {
      await h.page.locator('[data-submission-state="error"]').waitFor(); h.state.holds.add(phase); await h.page.clock.install();
      await h.page.getByRole('button', { name: 'Try again', exact: true }).evaluate(button => { button.click(); button.click(); button.click(); });
      await until(() => h.state.gates.has(phase), `provider ${phase} pending`); await h.page.clock.fastForward(30_100);
      const error = h.page.locator('[data-submission-state="error"]'); await error.waitFor({ timeout: 2500 }); assert.match(await error.innerText(), /Sign-in is taking longer/i);
      h.release(phase); await error.getByRole('button', { name: 'Try again', exact: true }).click();
    }
    await h.page.waitForURL(/\/runs\//); assert.equal(h.state.jobs.size, 1); assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
    await h.close(['pending OAuth receipt survives return', 'provider request deadline remains recoverable', 'one frozen submission']);
  }])),
  ['late-draft-restore-cannot-overwrite-run', async () => {
    const h = await harness('late-draft-restore-cannot-overwrite-run', { saved: true }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.waitForURL(/\/runs\//);
    const runUrl = h.page.url(); h.state.holds.add('restore');
    await h.page.evaluate(id => window.history.pushState(null, '', `/drafts/${id}`), uuid(1)); await until(() => h.state.gates.has('restore'), 'older draft restoration held');
    await h.page.evaluate(url => window.history.pushState(null, '', url), runUrl); await h.page.getByRole('region', { name: 'Submitted research inputs' }).waitFor();
    h.state.drafts.get(uuid(1)).composer.research_direction = 'A late mutable draft must not replace frozen inputs'; h.release('restore');
    await h.page.waitForTimeout(250); assert.equal(await h.page.getByText('A late mutable draft must not replace frozen inputs', { exact: true }).count(), 0);
    assert.equal(await h.page.locator('#research-direction').count(), 0); assert.equal(h.state.jobs.size, 1);
    await h.close(['same-component route race', 'late saved editor response cannot replace frozen run']);
  }],
  ...['anonymous', 'draft'].map(phase => [`editor-${phase}-failure-retry`, async () => {
    const h = await harness(`editor-${phase}-failure-retry`, { anonymous: phase === 'anonymous', fail: phase });
    await h.page.goto(`${origin}/?gap=${encodeURIComponent(gap.object.id)}`);
    await h.page.getByRole('button', { name: 'Retry opening editor', exact: true }).waitFor();
    await h.openContext(); await h.page.locator('#research-context').fill('Retain my working text through opening recovery');
    await h.page.getByRole('button', { name: 'Retry opening editor', exact: true }).click(); await h.page.waitForURL(/\/drafts\//); await h.openContext(); await h.readyToSubmit();
    assert.equal(await h.page.locator('#research-context').inputValue(), 'Retain my working text through opening recovery');
    const path = phase === 'anonymous' ? '/api/session/anonymous' : '/api/backend/v1/drafts';
    const attempts = h.state.calls.filter(call => call.method === 'POST' && call.path === path); assert.equal(attempts.length, 2); assert.equal(attempts[0].key, attempts[1].key); assert.deepEqual(attempts[0].body, attempts[1].body);
    assert.equal(h.state.drafts.size, 1); assert.equal(h.state.jobs.size, 0); await h.close(['opening error has actionable retry', 'typed context retained', 'same create key']);
  }]),
  ['boot-effect-replay-retains-recoverable-error', async () => {
    const h = await harness('boot-effect-replay-retains-recoverable-error', { saved: true, fail: 'job' }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); const error = h.page.locator('[data-submission-state="error"]'); await error.waitFor();
    // This development-only check intentionally replays React's existing boot
    // effect with refs intact, matching Fast Refresh's former stuck-spinner bug.
    const count = await h.page.evaluate(() => {
      const main = document.querySelector('main'); let fiber = main[Object.keys(main).find(key => key.startsWith('__reactFiber$'))];
      while (fiber && fiber.type?.name !== 'Composer') fiber = fiber.return;
      if (!fiber) throw new Error('Composer fiber unavailable for controlled effect replay');
      let hook = fiber.memoizedState, count = 0;
      while (hook) { const effect = hook.memoizedState; if (typeof effect?.create === 'function' && effect.create.toString().includes('restoreSubmission')) { effect.create(); count++; } hook = hook.next; } return count;
    });
    assert.equal(count, 1); await error.waitFor(); assert.equal(h.state.calls.filter(call => call.method === 'POST' && call.path.endsWith('/jobs')).length, 1);
    await error.getByRole('button', { name: 'Retry', exact: true }).click(); await h.page.waitForURL(/\/runs\//);
    const attempts = h.state.calls.filter(call => call.method === 'POST' && call.path.endsWith('/jobs')); assert.equal(attempts.length, 2); assert.equal(attempts[0].key, attempts[1].key);
    await h.close(['Fast Refresh effect replay retains error and Retry', 'no silent resubmission', 'same key on explicit retry']);
  }],
  ['explicit-run-beats-stale-pending-intent', async () => {
    const h = await harness('explicit-run-beats-stale-pending-intent', { saved: true, loseJob: true }); await h.openSaved(); await h.readyToSubmit();
    await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click(); await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).click(); await h.page.locator('[data-submission-state="error"]').waitFor();
    await h.page.goto(`${origin}/runs/${[...h.state.jobs.keys()][0]}`); await h.page.getByRole('region', { name: 'Submitted research inputs' }).waitFor();
    assert.equal(h.state.calls.filter(call => call.method === 'POST' && call.path.endsWith('/jobs')).length, 1);
    assert.equal(await h.page.locator('[data-submission-state]').count(), 0); await h.close(['explicit run URL wins over browser receipt', 'frozen request loaded without dispatch']);
  }],
  ...['google', 'orcid'].map(provider => [`${provider}-late-oauth-response-after-back`, async () => {
    const h = await harness(`${provider}-late-oauth-response-after-back`, { anonymous: true }); await h.seedPending(provider);
    // Deliberately emulate a transport that ignores cancellation, so the late
    // redirect response genuinely reaches application code after the deadline.
    await h.page.addInitScript(provider => { const original = window.fetch.bind(window); window.fetch = async (input, init) => {
      const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (path === `/api/auth/signin/${provider}`) { const response = await original(input, { ...init, signal: undefined }); window.__lateProviderDelivered = true; return response; }
      return original(input, init);
    }; }, provider);
    await h.page.goto(origin); await h.page.locator('[data-submission-state="error"]').waitFor(); await h.page.clock.install(); h.state.holds.add('oauth');
    await h.page.getByRole('button', { name: 'Try again', exact: true }).click(); await until(() => h.state.gates.has('oauth'), 'provider held'); await h.page.clock.fastForward(30_100);
    await h.page.locator('[data-submission-state="error"]').waitFor(); await h.page.getByRole('button', { name: 'Back to question', exact: true }).click();
    await h.openLegacy(); h.release('oauth'); await h.page.waitForFunction(() => window.__lateProviderDelivered === true);
    await h.page.waitForTimeout(100); assert.equal(new URL(h.page.url()).searchParams.has('oauth-fixture-return'), false); assert.equal(h.state.jobs.size, 0);
    assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
    await h.close(['late provider redirect cannot navigate after Back', 'abandoned pre-dispatch receipt cleared', 'no run dispatched']);
  }]),
];

try {
  const selected = process.env.DRAFT_LIFECYCLE_SCENARIO_FILTER ? scenarios.filter(([name]) => new RegExp(process.env.DRAFT_LIFECYCLE_SCENARIO_FILTER).test(name)) : scenarios;
  assert.ok(selected.length, 'Scenario filter must match at least one current lifecycle scenario');
  for (const [name, run] of selected) { process.stdout.write(`${name}…\n`); await run(); }
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.failure = error.stack || error.message; const h = activeHarnesses.at(-1); if (h) { report.failedCalls = h.state.calls; report.pageErrors = h.state.errors; report.unexpected = h.state.unexpected; await h.page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {}); report.pageText = await h.page.locator('body').innerText().catch(() => ''); } throw error; }
finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
