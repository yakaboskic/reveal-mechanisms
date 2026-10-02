#!/usr/bin/env node
/** Mocked publication/OAuth transitions; never authenticates or publishes real records. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '../../..');
const origin = new URL(process.env.PUBLICATION_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname));
const output = resolve(root, '.runtime/publication-signin/browser'); await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const spec = JSON.parse(await readFile(resolve(root, 'api/openapi.json'), 'utf8'));
const example = path => structuredClone(Object.values(spec.paths[path].get.responses['200'].content['application/json'].examples)[0].value);
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
const browser = await chromium.launch({ headless: true });
const report = { scope: 'Publication and OAuth responses mocked. Only provider availability is read from the live app.', scenarios: [] };
try {
  const probe = await browser.newContext();
  const providers = await (await probe.request.get(`${origin}/api/auth/providers`)).json();
  report.liveGoogleConfigured = !!providers.google; await probe.close();
  for (const kind of ['account', 'exploration']) for (const flow of ['google-desktop', 'google-mobile', 'existing-account-claim']) {
    const context = await browser.newContext({ viewport: { width: flow.endsWith('mobile') ? 390 : 1280, height: 980 }, serviceWorkers: 'block' });
    const page = await context.newPage(); page.setDefaultTimeout(12000);
    const record = kind === 'account' ? structuredClone(fixture.account) : example('/v1/analysis-outcomes/{outcome_id}');
    const id = kind === 'account' ? record.root_id : record.id;
    const resource = `/api/backend/v1/${kind === 'account' ? 'accounts' : 'analysis-outcomes'}/${id}`;
    const target = `${origin}/${kind === 'account' ? 'accounts' : 'analyses'}/${encodeURIComponent(id)}?view=conclusions#publication`;
    const claiming = flow === 'existing-account-claim';
    const state = { principalKind: claiming ? 'registered' : 'anonymous', canClaim: claiming, reads: 0, writes: [], signins: [], errors: [], unexpected: [] };
    record.publication = { visibility: 'private', version: 0, can_manage: !claiming, published_at: null, updated_at: null, has_unpublished_changes: false };
    page.on('pageerror', error => state.errors.push(error.message));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname);
      if (url.origin !== origin) { state.unexpected.push(url.origin + path); return route.abort(); }
      if (!path.startsWith('/api/')) return route.continue();
      const respond = json => route.fulfill({ status: 200, json });
      if (path === '/api/session/status') return respond({ principal: { user_id: '11111111-1111-4111-8111-111111111111' }, canClaim: state.canClaim, providers: { google: true, orcid: false } });
      if (path === '/api/backend/v1/me') return respond({ user_id: '11111111-1111-4111-8111-111111111111', principal_kind: state.principalKind, display_name: state.principalKind === 'registered' ? 'Signed-in researcher' : null, workspace_expires_at: null });
      if (path === '/api/auth/providers') return respond({ google: { id: 'google', name: 'Google', type: 'oauth', signinUrl: `${origin}/api/auth/signin/google`, callbackUrl: `${origin}/api/auth/callback/google` } });
      if (path === '/api/auth/csrf') return respond({ csrfToken: 'mock-csrf' });
      if (path === '/api/auth/signin/google') {
        const callback = new URLSearchParams(request.postData()).get('callbackUrl'); state.signins.push(callback);
        state.principalKind = 'registered'; return respond({ url: callback });
      }
      if (path === '/api/session/claim') { state.canClaim = false; record.publication.can_manage = true; return respond({ claimed: true }); }
      if (path === resource) {
        state.reads++;
        if (state.canClaim) return route.fulfill({ status: 404, json: { code: 'NOT_FOUND', detail: 'Private record belongs to the anonymous workspace.' } });
        return respond(record);
      }
      if (path === resource + '/publication') {
        if (request.method() === 'POST') { state.writes.push(request.postDataJSON()); record.publication = { ...record.publication, visibility: state.writes.at(-1).visibility, version: 1 }; }
        return respond(record.publication);
      }
      if (path.startsWith('/api/backend/v1/claims/')) return respond(fixture.claim);
      state.unexpected.push(request.method() + ' ' + path); return route.fulfill({ status: 500, json: { detail: 'Unmocked API blocked' } });
    });
    await page.goto(target);
    const panel = page.locator(kind === 'account' ? '.account-publication' : '.outcome-publication');
    if (claiming) {
      await page.getByText('Private record belongs to the anonymous workspace.', { exact: true }).waitFor();
      const before = state.reads;
      await page.getByRole('button', { name: 'Move my anonymous work', exact: true }).click();
      await panel.waitFor(); assert.ok(state.reads > before, 'Claim reloads record even when registered user ID stays the same');
    } else {
      if (kind === 'account') await panel.getByRole('button', { name: 'Publish', exact: true }).click();
      await panel.getByText(`Sign in to publish this ${kind === 'account' ? 'scientific account' : 'exploration'}.`, { exact: false }).waitFor();
      assert.equal(await panel.getByRole('button', { name: kind === 'account' ? 'Publish account' : 'Publish exploration', exact: true }).count(), 0);
      assert.equal(state.writes.length, 0);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: resolve(output, `${kind}-${flow}.png`), fullPage: true });
      await panel.getByRole('button', { name: 'Continue with Google', exact: true }).click();
      await panel.getByRole('button', { name: kind === 'account' ? 'Publish' : 'Publish exploration…', exact: true }).waitFor();
      assert.deepEqual(state.signins, [target]); assert.equal(page.url(), target);
    }
    assert.equal(state.writes.length, 0, 'Sign-in or claim does not automatically publish');
    await panel.getByRole('button', { name: kind === 'account' ? 'Publish' : 'Publish exploration…', exact: true }).click();
    assert.equal(state.writes.length, 0, 'Opening confirmation does not publish');
    await panel.getByRole('button', { name: kind === 'account' ? 'Publish account' : 'Publish exploration', exact: true }).click();
    await panel.getByRole('button', { name: 'Unpublish', exact: true }).waitFor();
    assert.deepEqual(state.writes, [{ visibility: 'public', expected_version: 0 }]);
    assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []);
    report.scenarios.push({ kind, flow, passed: true }); await context.close();
  }
  report.status = 'passed'; console.log(JSON.stringify(report));
} catch (error) { report.status = 'failed'; report.error = String(error); throw error; }
finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2)); await browser.close(); }
