#!/usr/bin/env node
/** Mocked browser check for explicit OAuth consent and anonymous-workspace promotion. No backend, model or external request is allowed. */
import assert from 'node:assert/strict';
import { access, mkdir, readdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
let playwright;
try { playwright = await import('playwright'); }
catch { playwright = await import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
let executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
if (!executablePath) for (const name of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
  const candidate = resolve(homedir(), 'Library/Caches/ms-playwright', name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
  try { await access(candidate); executablePath = candidate; break; } catch {}
}
const browser = await playwright.chromium.launch({ headless: true, executablePath });
const origin = process.env.RESEARCH_CONSENT_FRONTEND || 'http://127.0.0.1:3000';
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const context = await browser.newContext({ viewport: { width: 1100, height: 1000 }, serviceWorkers: 'block' });
const page = await context.newPage(), unexpected = [], errors = [], calls = [];
const workId = '11111111-1111-4111-8111-111111111111', secondId = '22222222-2222-4222-8222-222222222222';
const user = '33333333-3333-4333-8333-333333333333', code = 'ABCD-EFGH', requestId = 'canonical-request-123';
const work = id => ({ id, state: 'ready', request: { composer: { research_direction: id === workId ? 'Frozen anonymous gap' : 'Another owned gap' } }, grants: [], submissions: [], created_at: new Date().toISOString() });
let registered = false, canClaim = false, providers = { google: false, orcid: false }, owned = [], decisions = [], claims = 0;
let consent = { request_id: requestId, kind: 'device', client_name: 'Reveal local launcher', client_id: 'registered-client', scopes: ['research:read', 'research:write'], resource: 'https://reveal.example/mcp', requested_local_work_id: workId, expires_at: new Date(Date.now() + 600000).toISOString(), status: 'pending', requires_registered: true };
const callback = `http://127.0.0.1:${new URL(origin).port || '3000'}/client/callback?error=access_denied&state=exact%2Fstate+value`;
page.on('pageerror', error => errors.push(error.message));
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
  if (request.url() === callback) return route.fulfill({ contentType: 'text/html', body: '<h1>Returned to client</h1>' });
  if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
  if (!path.startsWith('/api/')) return route.continue();
  const body = request.headers()['content-type']?.includes('application/json') ? request.postDataJSON() : request.postData();
  calls.push({ path, method, body, query: url.search });
  const json = value => route.fulfill({ json: structuredClone(value) });
  if (path === '/api/session/status') return json({ principal: { user_id: user }, canClaim, canAdmin: false, providers });
  if (path === '/api/backend/v1/me') return json({ user_id: user, principal_kind: registered ? 'registered' : 'anonymous', display_name: 'Consent fixture', email: null, person: null });
  if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': fixture\n\n' });
  if (path === '/api/backend/v1/local-work') return json({ items: owned.map(work), page: { next_cursor: null } });
  if (path === '/api/auth/session') return json({});
  if (path === '/api/auth/providers') return json({ google: { id: 'google', name: 'Google', type: 'oauth', signinUrl: `${origin}/api/auth/signin/google`, callbackUrl: `${origin}/api/auth/callback/google` } });
  if (path === '/api/auth/csrf') return json({ csrfToken: 'fixture-csrf' });
  if (path === '/api/auth/signin/google') {
    const data = new URLSearchParams(request.postData());
    assert.equal(data.get('callbackUrl'), `${origin}/research/connect?user_code=${code}`);
    assert.deepEqual([...data.keys()].sort(), ['callbackUrl', 'csrfToken', 'json']);
    registered = true; canClaim = true;
    return json({ url: data.get('callbackUrl') });
  }
  if (path === '/api/session/claim') {
    assert.equal(registered, true); assert.equal(method, 'POST'); assert.deepEqual(body, { consent: true }); assert.ok(request.headers()['idempotency-key']);
    claims++; canClaim = false; owned = [workId]; return json({ moved: true });
  }
  if (path === '/api/backend/v1/research-oauth/consent') {
    assert.equal(registered, true, 'Anonymous views never retrieve private consent requests');
    if (method === 'GET') return json(consent);
    assert.equal(method, 'POST'); assert.equal(body.request_id, requestId); decisions.push(body);
    return consent.kind === 'authorization_code' ? json({ redirect_url: callback }) : json({ approved: body.approve, ...(body.approve ? { local_work_id: body.local_work_id } : {}) });
  }
  unexpected.push(`${method} ${path}`); return route.abort();
});
try {
  await page.goto(`${origin}/research/connect?user_code=${code}`);
  await page.getByRole('heading', { name: 'Sign in to approve access' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Continue with Google', exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: 'Continue anonymously', exact: true }).count(), 0);
  assert.equal(calls.some(call => call.path.includes('research-oauth')), false);
  providers = { google: true, orcid: false }; await page.reload();
  await page.getByRole('button', { name: 'Continue with Google', exact: true }).click();
  await page.getByRole('heading', { name: 'Reveal local launcher requests access' }).waitFor();
  await page.getByText('The requested research is not in your available workspace.', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Approve connection', exact: true }).isDisabled(), true);
  assert.equal(claims, 0); assert.deepEqual(decisions, []);
  await page.getByRole('button', { name: 'Move my anonymous research', exact: true }).click();
  await page.getByText('Frozen anonymous gap', { exact: true }).waitFor();
  assert.equal(claims, 1); assert.deepEqual(decisions, []);
  await page.reload(); await page.getByText('Frozen anonymous gap', { exact: true }).waitFor();
  assert.deepEqual(decisions, [], 'Refreshing or viewing consent never approves a connection');
  await page.getByRole('button', { name: 'Approve connection', exact: true }).click();
  await page.getByRole('heading', { name: 'Connection approved' }).waitFor();
  assert.deepEqual(decisions, [{ request_id: requestId, approve: true, local_work_id: workId }]);
  assert.equal(await page.getByRole('button', { name: 'Approve connection', exact: true }).count(), 0);
  consent = { ...consent, expires_at: new Date(Date.now() - 1000).toISOString() };
  await page.reload(); await page.getByText(/This connection request has expired/).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Approve connection', exact: true }).count(), 0);
  consent = { ...consent, kind: 'authorization_code', requested_local_work_id: null, expires_at: new Date(Date.now() + 600000).toISOString(), redirect_uri: callback.split('?')[0] };
  owned = [workId, secondId]; decisions = [];
  await page.goto(`${origin}/research/connect?request_id=request-from-client&redirect_url=https://untrusted.example`);
  await page.getByLabel('Choose the research this agent may access').waitFor();
  assert.equal(await page.getByRole('button', { name: 'Approve connection', exact: true }).isDisabled(), true);
  await page.getByLabel('Choose the research this agent may access').selectOption(secondId);
  assert.equal(await page.getByRole('button', { name: 'Approve connection', exact: true }).isEnabled(), true);
  await page.getByRole('button', { name: 'Decline', exact: true }).click();
  await page.getByRole('heading', { name: 'Returned to client' }).waitFor();
  assert.equal(page.url(), callback, 'Server callback and exact OAuth state are retained');
  assert.deepEqual(decisions, [{ request_id: requestId, approve: false }]);
  assert.equal(calls.some(call => /\/grants$|\/jobs$|\/oauth\/token/.test(call.path)), false);
  assert.deepEqual(await page.evaluate(() => ({ ...localStorage })), {});
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
  console.log('Consent: missing providers, preserved sign-in callback, explicit anonymous ownership promotion, no automatic grants, device approval, expiry, work selection, and exact authorization-code denial callback passed.');
} finally { await context.close(); await browser.close(); }
