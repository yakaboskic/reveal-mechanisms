#!/usr/bin/env node
/** Real browser UI, fully intercepted APIs. Start the local frontend with
 * NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED=true, then set LIGHTNING_BASE_URL if needed.
 * LIGHTNING_EXPECT_DISABLED=true checks a build with the feature disabled. */
import assert from 'node:assert/strict';
import { readFile, access, readdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.LIGHTNING_BASE_URL || 'http://127.0.0.1:3266').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Use a local frontend');
const disabled = process.env.LIGHTNING_EXPECT_DISABLED === 'true';
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0], factor = fixture.suggestions.automatic_anchors[0].factor;
const user = '11111111-1111-4111-8111-111111111111', stamp = '2026-10-09T12:00:00Z';
const uuid = n => `22222222-2222-4222-8222-${String(n).padStart(12, '0')}`;
const pageOf = items => ({ items, page: { next_cursor: null, has_more: false, snapshot_id: 'mock' } });
const composer = () => ({ source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision },
  eaggl_anchors: [{ reference: { source: 'eaggl', source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }],
  dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: [], research_direction: '', context: 'Saved context', hypotheses: '', upload_ids: [] });
const auditResult = { assessment: 'partial', summary: 'A plausible connection needs further investigation.', observations: [{ text: 'The observed loading supports a candidate association.', evidence_refs: ['E1'] }],
  recommended_direction: 'Investigate the candidate association.', missing_evidence: ['Independent causal evidence'], next_steps: ['Check the relevant tissue context.'], limitations: ['Only the supplied evidence was assessed.'] };
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  for (const name of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(value => /^chromium-\d+$/.test(value)).sort().reverse()) {
    const path = resolve(homedir(), 'Library/Caches/ms-playwright', name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* Try another installed browser. */ }
  }
}
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
async function until(check, description) {
  const start = Date.now(); while (!await check()) { assert.ok(Date.now() - start < 20000, `Timed out: ${description}`); await new Promise(resolve => setTimeout(resolve, 30)); }
}
async function harness(options = {}) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 950 }, serviceWorkers: 'block', reducedMotion: 'reduce' });
  const page = await context.newPage(); page.setDefaultTimeout(20000);
  const state = { drafts: new Map(), audits: new Map(), jobs: new Map(), local: new Map(), frozen: new Map(), uploads: new Map(), receipts: new Map(), calls: [], errors: [], unexpected: [], sequence: 10, lostAudit: false, lostContinuation: false, rejected: false, signed: true, owner: user };
  if (options.saved) state.drafts.set(uuid(1), { id: uuid(1), owner_user_id: user, lifecycle: 'saved', name: 'Named research draft', version: 1, composer: composer(), created_at: stamp, updated_at: stamp, expires_at: null });
  let releaseDraft;
  const draftGate = options.holdDraft ? new Promise(resolve => { releaseDraft = resolve; }) : Promise.resolve();
  page.on('pageerror', error => state.errors.push(error.message));
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname), method = request.method();
    if (url.origin !== origin) { state.unexpected.push(`${method} ${url.href}`); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    const body = request.postData() ? request.headers()['content-type']?.includes('application/x-www-form-urlencoded') ? Object.fromEntries(new URLSearchParams(request.postData())) : request.postDataJSON() : undefined;
    const key = request.headers()['idempotency-key']; state.calls.push({ method, path, body: structuredClone(body), key });
    const respond = (value, status = 200) => route.fulfill({ status, json: structuredClone(value) }).catch(error => { if (!/closed|cancel|handled/i.test(error.message)) throw error; });
    const fail = (status, code, detail) => respond({ code, detail }, status);
    const receiptKey = `${method}:${path}:${key}`;
    const receipt = () => state.receipts.get(receiptKey);
    const remember = value => { assert.ok(key); state.receipts.set(receiptKey, structuredClone(value)); return value; };
    if (path === '/api/session/status') return respond({ principal: { user_id: state.owner, principal_kind: 'registered' }, canClaim: false, canAdmin: false, providers: { google: true, orcid: true } });
    if (path === '/api/auth/session') return respond({});
    if (path === '/api/backend/v1/me') return respond({ user_id: state.owner, principal_kind: 'registered', display_name: 'Mock researcher', workspace_expires_at: null });
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': mock heartbeat\n\n' });
    if (path === '/api/backend/v1/knowledge-gaps') return respond(pageOf([gap]));
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return respond(gap);
    if (path.endsWith('/vote')) return respond(gap.votes || { upvotes: 0, downvotes: 0, score: 0, user_vote: null });
    if (/\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(path)) return respond(pageOf([]));
    if (path === '/api/backend/v1/mechanisms/suggest') return respond(fixture.suggestions);
    if (path.startsWith('/api/backend/v1/mechanisms/')) return respond(factor);
    if (path === '/api/backend/v1/me/explorations') return respond(method === 'POST' ? { ...body, knowledge_gap: gap.object, last_explored_at: stamp } : pageOf([]));
    if (path.includes('/cfde-assessments')) return fail(503, 'MOCK_JEV_UNAVAILABLE', 'Mock check unavailable; research remains available.');
    if (path === '/api/backend/v1/drafts' && method === 'GET') return respond(pageOf([...state.drafts.values()].filter(value => value.lifecycle === 'saved')));
    if (path === '/api/backend/v1/drafts' && method === 'POST') {
      await draftGate; if (receipt()) return respond(receipt(), 201);
      const value = { ...body, id: uuid(state.sequence++), version: 1, owner_user_id: user, lifecycle: body.lifecycle || 'saved', created_at: stamp, updated_at: stamp, expires_at: null };
      state.drafts.set(value.id, structuredClone(value)); return respond(remember(value), 201);
    }
    const draft = /^\/api\/backend\/v1\/drafts\/([^/]+)$/.exec(path);
    if (draft) {
      const value = state.drafts.get(draft[1]); if (!value) return fail(404, 'NOT_FOUND', 'Draft is unavailable');
      if (method === 'DELETE') { state.drafts.delete(value.id); return respond({ id: value.id, deleted: true }); }
      if (method === 'PATCH') Object.assign(value, body, { version: value.version + 1 });
      return respond(value);
    }
    if (path === '/api/backend/v1/lightning-audits' && method === 'POST') {
      if (receipt()) return respond(receipt(), 202);
      if (options.rejectAudit && !state.rejected) { state.rejected = true; return fail(429, 'LIGHTNING_BUSY', 'Lightning audits are busy. Try again shortly.'); }
      const source = state.drafts.get(body.draft_id); assert.ok(source); assert.equal(source.version, body.draft_version);
      const id = uuid(state.sequence++), requestId = uuid(state.sequence++);
      const audit = { id, kind: 'lightning_audit', status: 'succeeded', research_request_id: requestId, source_draft_id: source.id, source_draft_version: source.version,
        question: { id: gap.object.id, text: gap.object.text }, created_at: stamp, updated_at: stamp, completed_at: stamp, continuation_expires_at: '2099-11-08T12:00:00Z', reference_generation_id: 'mock-generation',
        result: auditResult, coverage: { missing: ['A tissue annotation'], truncations: [{ source: 'loading', included_chars: 50, total_chars: 100 }] },
        evidence_references: [{ id: 'E1', pointer: '/factors/0/genes/0', label: 'Supplied loading', value: 'Original retained evidence', source: { revision: 'mock-revision' } }],
        provenance: { model: 'mock-only', prompt_version: 'mock-v1' }, usage: { input_tokens: 100, output_tokens: 50 }, error: null, continuations: [] };
      state.audits.set(id, structuredClone(audit)); state.frozen.set(requestId, { id: requestId, composer: structuredClone(source.composer), question_id: gap.object.id, document: { knowledge_gaps: [gap.object] } }); remember(audit);
      if (options.loseAudit && !state.lostAudit) { state.lostAudit = true; return route.abort('failed'); }
      return respond(audit, 202);
    }
    if (path === '/api/backend/v1/lightning-audits') return respond(pageOf([...state.audits.values()]));
    const auditPath = /^\/api\/backend\/v1\/lightning-audits\/([^/]+)(\/continue)?$/.exec(path);
    if (auditPath) {
      const audit = state.audits.get(auditPath[1]); if (!audit) return fail(404, 'NOT_FOUND', 'Audit is unavailable');
      if (!auditPath[2]) return respond(audit);
      if (receipt()) return respond(receipt(), 201);
      if (options.rejectContinuation && !state.rejected) { state.rejected = true; return fail(429, 'LOCAL_WORK_LIMIT', 'Close an earlier local research run first.'); }
      const run = { mode: body.mode, id: uuid(state.sequence++), research_request_id: uuid(state.sequence++), created_at: stamp };
      const frozen = { ...state.frozen.get(audit.research_request_id), id: run.research_request_id, lightning_audit_id: audit.id };
      frozen.composer = { ...frozen.composer, research_direction: body.research_direction }; state.frozen.set(frozen.id, frozen);
      if (body.mode === 'online') state.jobs.set(run.id, { id: run.id, owner_user_id: user, kind: 'analysis', status: 'failed', stage: 'complete', research_request_id: frozen.id, input_account_id: null, created_at: stamp, updated_at: stamp, completed_at: stamp, result: null, failure: { code: 'MOCK', message: 'No agent dispatched.', retryable: false }, warnings: [], last_event_id: '0', links: { self: `/v1/jobs/${run.id}`, events: `/v1/jobs/${run.id}/events` } });
      else state.local.set(run.id, { id: run.id, state: 'ready', created_at: stamp, request: frozen, grants: [], submissions: [], package_id: 'package', package_sha256: 'mock' });
      audit.continuations.push(run); remember(run);
      if (options.loseContinuation && !state.lostContinuation) { state.lostContinuation = true; return route.abort('failed'); }
      return respond(run, 201);
    }
    if (path === '/api/backend/v1/jobs') return respond(pageOf([...state.jobs.values()]));
    const job = /^\/api\/backend\/v1\/jobs\/([^/]+)(\/events)?$/.exec(path);
    if (job) return job[2] ? route.fulfill({ contentType: 'text/event-stream', body: '' }) : respond(state.jobs.get(job[1]));
    if (path === '/api/backend/v1/local-work') return respond(pageOf([...state.local.values()]));
    if (path.startsWith('/api/backend/v1/local-work/')) return respond(state.local.get(path.split('/').at(-1)));
    if (path === '/api/backend/v1/research-requests') return respond(pageOf([...state.frozen.values()]));
    if (path.startsWith('/api/backend/v1/research-requests/')) return respond(state.frozen.get(path.split('/').at(-1)));
    if (path === '/api/backend/v1/accounts' || path === '/api/backend/v1/analysis-outcomes') return respond(pageOf([]));
    if (path === '/api/backend/v1/uploads' && method === 'POST') {
      const id = uuid(state.sequence++), upload = { ...body, id, media_type: 'text/plain', status: 'pending', created_at: stamp, expires_at: '2099-11-08T12:00:00Z', storage: null, extraction: null, error: null };
      state.uploads.set(id, upload); return respond({ upload, transfer: { method: 'POST', url: `/v1/uploads/${id}/content`, fields: {}, encoding: 'base64' } }, 201);
    }
    const upload = /^\/api\/backend\/v1\/uploads\/([^/]+)(?:\/(content|complete))?$/.exec(path);
    if (upload) { const value = state.uploads.get(upload[1]); assert.ok(value); if (upload[2] === 'complete') value.status = 'ready'; return respond(value); }
    state.unexpected.push(`${method} ${path}`); return fail(500, 'UNMOCKED', 'Unexpected API request blocked');
  });
  return { page, state, releaseDraft,
    async open() { await page.goto(`${origin}${options.saved ? `/drafts/${uuid(1)}` : `/?gap=${encodeURIComponent(gap.object.id)}`}`); await page.locator('.selected-question').waitFor(); },
    async launch() { const trigger = page.getByRole('button', { name: 'Let’s close this gap', exact: true }); await until(() => trigger.isEnabled(), 'launch is ready'); await trigger.click(); await page.getByRole('menuitem', { name: 'Lightning audit', exact: false }).click(); },
    async complete() { await page.waitForURL(/\/lightning-audits\//); await page.getByRole('heading', { name: 'Limited or partial direction' }).waitFor(); },
    async close() { assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []); await context.close(); },
  };
}
const auditPosts = state => state.calls.filter(call => call.method === 'POST' && call.path === '/api/backend/v1/lightning-audits');
const continuationPosts = state => state.calls.filter(call => call.method === 'POST' && call.path.endsWith('/continue'));
try {
  if (disabled) {
    const h = await harness(); await h.open(); const trigger = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true });
    await until(() => trigger.isEnabled(), 'launch is ready'); await trigger.focus(); await trigger.press('ArrowUp');
    assert.equal(await h.page.getByRole('menuitem').count(), 2); assert.equal(await h.page.getByRole('menuitem', { name: 'Lightning audit', exact: false }).count(), 0);
    assert.equal(await h.page.evaluate(() => document.activeElement.textContent), 'Use my local agentWork with an agent on your computer.');
    await h.close(); console.log('PASS: disabled feature retains two modes and keyboard navigation');
  } else {
    {
      const h = await harness(); await h.open();
      const trigger = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }); await until(() => trigger.isEnabled(), 'launch is ready');
      await trigger.focus(); await trigger.press('ArrowDown');
      assert.match(await h.page.evaluate(() => document.activeElement.textContent), /^Lightning auditRecommended/);
      const choices = h.page.getByRole('menuitem');
      assert.deepEqual(await choices.locator('.research-mode-label').allTextContents(), ['Lightning auditRecommended', 'Run online', 'Use my local agent']);
      assert.equal(await choices.locator('svg[aria-hidden="true"]').count(), 3);
      assert.equal(await h.page.getByText('Recommended', { exact: true }).count(), 1);
      await h.page.keyboard.press('End'); assert.match(await h.page.evaluate(() => document.activeElement.textContent), /^Use my local agent/);
      await h.page.keyboard.press('ArrowDown'); assert.match(await h.page.evaluate(() => document.activeElement.textContent), /^Lightning audit/);
      await h.page.keyboard.press('Escape'); await trigger.press('ArrowUp'); assert.match(await h.page.evaluate(() => document.activeElement.textContent), /^Use my local agent/);
      await h.page.keyboard.press('Home'); assert.match(await h.page.evaluate(() => document.activeElement.textContent), /^Lightning audit/);
      await h.page.keyboard.press('Enter'); await h.complete(); assert.equal(auditPosts(h.state).length, 1);
      await h.page.getByRole('heading', { name: 'Rationale', exact: true }).waitFor();
      assert.equal(await h.page.locator('.lightning-header h1').evaluate(element => getComputedStyle(element).fontSize), '17px');
      await h.page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await h.page.locator('.lightning-header h1').evaluate(element => getComputedStyle(element).fontSize), '16px');
      await h.page.setViewportSize({ width: 1200, height: 950 });
      await h.page.getByRole('link', { name: 'E1', exact: true }).click(); await h.page.getByText('Supplied loading', { exact: false }).click();
      await h.page.getByText('Original retained evidence', { exact: true }).waitFor();
      const brief = h.page.getByLabel('Direction and next steps for the agent'); await brief.fill('Reviewed online direction');
      await h.page.reload(); await h.complete(); assert.equal(await brief.inputValue(), 'Reviewed online direction'); assert.equal(auditPosts(h.state).length, 1);
      if (process.env.LIGHTNING_SCREENSHOT) await h.page.screenshot({ path: process.env.LIGHTNING_SCREENSHOT, fullPage: true });
      await h.page.getByRole('button', { name: 'Run online', exact: true }).evaluate(button => { button.click(); button.click(); }); await h.page.waitForURL(/\/runs\//);
      await h.page.getByRole('link', { name: 'Initial Lightning audit' }).waitFor(); assert.equal(continuationPosts(h.state)[0].body.research_direction, 'Reviewed online direction');
      assert.equal(continuationPosts(h.state).length, 1, 'Double clicks create only one continuation');
      await h.page.goto(`${origin}/workspace?tab=runs`); await h.page.locator('[data-audit-id]').waitFor(); assert.equal(await h.page.locator('[data-audit-id]').count(), 1);
      await h.close(); console.log('PASS: fresh gap, three-item keyboard menu, evidence refs, read-only reload, retained edit, online handoff and mixed history');
    }
    {
      const h = await harness({ saved: true, loseAudit: true, loseContinuation: true }); const original = structuredClone(h.state.drafts.get(uuid(1)));
      await h.open(); await h.launch(); await h.page.locator('[data-submission-state="error"]').waitFor();
      await h.page.reload(); await h.complete(); assert.equal(h.state.audits.size, 1); assert.deepEqual(h.state.drafts.get(uuid(1)), original);
      const posts = auditPosts(h.state); assert.ok(posts.length >= 2); assert.ok(posts.every(call => call.key === posts[0].key && JSON.stringify(call.body) === JSON.stringify(posts[0].body)));
      await h.page.getByLabel('Direction and next steps for the agent').fill('Reviewed local direction');
      await h.page.getByRole('button', { name: 'Use my local agent', exact: true }).click(); await h.page.getByRole('button', { name: 'Check submission' }).waitFor();
      await until(() => h.page.getByRole('button', { name: 'Check submission' }).isEnabled(), 'uncertain handoff is retryable');
      for (const audit of h.state.audits.values()) audit.continuation_expires_at = '2020-01-01T00:00:00Z';
      await h.page.reload(); await h.complete(); assert.equal(continuationPosts(h.state).length, 1, 'Reload never automatically dispatches a paid run');
      assert.equal(await h.page.getByLabel('Direction and next steps for the agent').isDisabled(), true);
      await h.page.getByRole('button', { name: 'Check submission' }).click(); await h.page.waitForURL(/\/local-runs\//); await h.page.getByRole('link', { name: 'Initial Lightning audit' }).waitFor();
      const runs = continuationPosts(h.state); assert.equal(runs.length, 2); assert.deepEqual(runs[1], runs[0]); assert.equal(h.state.local.size, 1);
      await h.close(); console.log('PASS: saved draft preserved, ambiguous audit dispatch reconciled after reload, local handoff exact receipt without automatic retry');
    }
    {
      const h = await harness({ holdDraft: true }); await h.open();
      const details = h.page.locator('details.additional-context'); await details.locator(':scope > summary').click();
      await h.page.locator('input[type=file]').setInputFiles({ name: 'evidence.txt', mimeType: 'text/plain', buffer: Buffer.from('Supplied evidence') });
      await until(() => h.state.calls.some(call => call.path === '/api/backend/v1/drafts' && call.method === 'POST'), 'lazy draft creation starts');
      assert.equal(await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).isDisabled(), true);
      h.releaseDraft(); await until(() => [...h.state.uploads.values()].some(upload => upload.status === 'ready'), 'attachment completes');
      await h.launch(); await h.complete(); const audit = [...h.state.audits.values()][0], frozen = h.state.frozen.get(audit.research_request_id);
      assert.equal(frozen.composer.upload_ids.length, 1); assert.equal(h.state.uploads.size, 1);
      await h.close(); console.log('PASS: attachment waits for one lazy draft and blocks audit until verified');
    }
    {
      const h = await harness({ rejectContinuation: true }); await h.open(); await h.launch(); await h.complete();
      await h.page.getByRole('button', { name: 'Use my local agent', exact: true }).click();
      await h.page.getByRole('alert').filter({ hasText: 'Close an earlier' }).waitFor();
      assert.equal(await h.page.getByLabel('Direction and next steps for the agent').isDisabled(), false);
      assert.equal(await h.page.getByRole('button', { name: 'Check submission' }).count(), 0);
      await h.page.getByLabel('Direction and next steps for the agent').fill('Choose an online investigation instead.');
      await h.page.getByRole('button', { name: 'Run online', exact: true }).click(); await h.page.waitForURL(/\/runs\//);
      assert.equal(h.state.jobs.size, 1); assert.equal(h.state.local.size, 0);
      await h.close(); console.log('PASS: definitive quota rejection unlocks the brief and alternate continuation mode');
    }
    {
      const h = await harness({ rejectAudit: true }); await h.open(); await h.launch();
      await h.page.getByRole('alert').filter({ hasText: 'Lightning audits are busy' }).waitFor();
      assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
      const trigger = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }); await until(() => trigger.isEnabled(), 'modes unlock after admission rejection');
      await trigger.click(); assert.equal(await h.page.getByRole('menuitem', { name: 'Run online', exact: false }).isEnabled(), true); await h.page.keyboard.press('Escape');
      await h.launch(); await h.complete(); assert.equal(h.state.audits.size, 1);
      assert.notEqual(auditPosts(h.state)[0].key, auditPosts(h.state)[1].key);
      await h.close(); console.log('PASS: rejected audit admission keeps inputs editable and permits a fresh explicit attempt');
    }
    {
      const h = await harness({ loseAudit: true }); await h.open(); await h.launch(); await h.page.locator('[data-submission-state="error"]').waitFor();
      const original = await h.page.evaluate(() => JSON.parse(sessionStorage.getItem('reveal:submission')));
      h.state.owner = uuid(900); await h.page.reload();
      await h.page.getByRole('link', { name: 'Check workspace research runs' }).waitFor();
      assert.equal(auditPosts(h.state).length, 1, 'An owner transition must not repeat a paid audit');
      const retained = await h.page.evaluate(() => JSON.parse(sessionStorage.getItem('reveal:submission')));
      assert.equal(retained.owner, original.owner); assert.deepEqual(retained.submitKey, original.submitKey);
      await h.page.getByRole('link', { name: 'Check workspace research runs' }).click(); await h.page.locator('[data-audit-id]').waitFor();
      await h.close(); console.log('PASS: owner transition after a lost acknowledgement retains the original receipt and opens claimed workspace history without redispatch');
    }
    {
      const h = await harness(); await h.open(); await h.launch(); await h.complete();
      await h.page.evaluate(owner => { const channel = new BroadcastChannel(`reveal:workspace-events:${owner}`); channel.postMessage({ type: 'revoked', owner }); setTimeout(() => channel.close(), 100); }, user);
      await h.page.getByRole('alert').filter({ hasText: 'Workspace access changed' }).waitFor();
      await h.page.getByRole('button', { name: 'Refresh saved audit' }).click(); await h.complete();
      assert.match(await h.page.getByLabel('Direction and next steps for the agent').inputValue(), /Investigate the candidate/);
      assert.equal(auditPosts(h.state).length, 1);
      await h.close(); console.log('PASS: same-owner access reset clears private content and permits explicit read recovery with the suggested brief restored');
    }
  }
} finally { await browser.close(); }
