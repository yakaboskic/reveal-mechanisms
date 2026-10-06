#!/usr/bin/env node
/** Mocked browser check for both local-agent connection screens. No backend, model or external request is allowed. */
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
const origins = (process.env.LOCAL_WORK_FRONTENDS || 'http://127.0.0.1:3103,http://127.0.0.1:3104').split(',');
const fixtureZip = Buffer.from('UEsDBBQAAAAIALN7Rl1XWY0xLwAAAC4AAAAZAAAAcmV2ZWFsLWZpeHR1cmUvc2V0dXAuanNvbqtWys1PSVWyUkrMy8+rzM0vLVbSUcrJT07MiS/PL8qOz0wByqVlVpSUFqUq1QIAUEsDBBQAAAAIALN7Rl2YT40XOgAAADgAAAAYAAAAcmV2ZWFsLWZpeHR1cmUvUkVBRE1FLm1kc8usKCktSlUozy/KLi5ITE7VU/DLV0guSk1JzSvJTMxRyC9SyEkszUvOSC1SSK1ITS4tyczP0+MCAFBLAQIUAxQAAAAIALN7Rl1XWY0xLwAAAC4AAAAZAAAAAAAAAAAAAACAAQAAAAByZXZlYWwtZml4dHVyZS9zZXR1cC5qc29uUEsBAhQDFAAAAAgAs3tGXZhPjRc6AAAAOAAAABgAAAAAAAAAAAAAAIABZgAAAHJldmVhbC1maXh0dXJlL1JFQURNRS5tZFBLBQYAAAAAAgACAI0AAADWAAAAAAA=', 'base64');
try {
  for (const origin of origins) {
    assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, serviceWorkers: 'block' });
    await context.addInitScript(() => {
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      window.downloadUrls = { created: [], released: [] };
      URL.createObjectURL = blob => { const url = create(blob); window.downloadUrls.created.push(url); return url; };
      URL.revokeObjectURL = url => { window.downloadUrls.released.push(url); revoke(url); };
    });
    const page = await context.newPage(), errors = [], unexpected = [], calls = [], downloads = [];
    const id = '11111111-1111-4111-8111-111111111111', user = '22222222-2222-4222-8222-222222222222';
    const expires = new Date(Date.now() + 3600000).toISOString();
    const work = { id, state: 'preparing', request: { question_id: 'gap', document: { knowledge_gaps: [{ id: 'gap', text: 'How does this mechanism explain the gap?' }] } }, package_id: 'package', package_sha256: 'a'.repeat(64), grants: [{ grant_id: 'another-agent', expires_at: expires, revoked_at: null }], submissions: [{ id: 'submission', state: 'accepted', created_at: new Date().toISOString(), account_ids: [], reused_account_ids: ['existing-account'], report: { accepted: true } }, { id: 'validation', state: 'succeeded', validation_only: true, account_ids: ['candidate-account'] }], created_at: new Date().toISOString(), last_activity: new Date().toISOString(), last_action: 'seed_prepared' };
    let denied = false;
    let setupFailure = false, holdSetup = false, releaseSetup;
    page.on('pageerror', error => errors.push(error.message)); page.on('download', download => downloads.push(download));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
      if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
      if (!path.startsWith('/api/')) return route.continue();
      calls.push({ path, method, key: request.headers()['idempotency-key'], body: request.postDataJSON() });
      const json = value => route.fulfill({ json: structuredClone(value) });
      if (path === '/api/session/status') return json({ principal: { user_id: user }, canClaim: false, canAdmin: false, providers: {} });
      if (path === '/api/auth/session') return json({});
      if (path === '/api/backend/v1/me') return json({ user_id: user, principal_kind: 'anonymous', display_name: 'Local fixture', email: null, person: null });
      if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': fixture\n\n' });
      if (path === '/api/backend/v1/local-work') return json({ items: [work], page: { next_cursor: null } });
      if (path === `/api/backend/v1/local-work/${id}`) return denied ? route.fulfill({ status: 403, json: { code: 'FORBIDDEN', detail: 'Access revoked.' } }) : json(work);
      if (path === `/api/backend/v1/local-work/${id}/package`) return json({ package: {}, manifest: {}, artifacts: [] });
      if (path === `/api/backend/v1/local-work/${id}/setup-kit` && method === 'POST') {
        assert.deepEqual(Object.keys(request.postDataJSON()), ['client']);
        assert.ok(['codex', 'claude_code'].includes(request.postDataJSON().client));
        if (setupFailure) { setupFailure = false; return route.fulfill({ status: 503, json: { detail: 'Workspace preparation unavailable. Try again.' } }); }
        if (holdSetup) { holdSetup = false; await new Promise(resolve => { releaseSetup = resolve; }); }
        return route.fulfill({ contentType: 'application/zip', headers: { 'Cache-Control': 'private, no-store', 'Content-Disposition': `attachment; filename="reveal-${id}.zip"` }, body: fixtureZip }).catch(() => {});
      }
      if (path.includes('/grants/') && method === 'DELETE') { if (denied) return route.fulfill({ status: 403, json: { code: 'FORBIDDEN', detail: 'Access revoked.' } }); work.grants.find(grant => path.endsWith(grant.grant_id)).revoked_at = new Date().toISOString(); return route.fulfill({ status: 204 }); }
      if (path.endsWith('/close') && method === 'POST') { assert.ok(request.headers()['idempotency-key']); work.state = 'closed'; return json(work); }
      unexpected.push(`${method} ${path}`); return route.abort();
    });
    const saveDownload = async (button, expected, filename) => {
      const pending = page.waitForEvent('download'); await button.click(); const download = await pending, chunks = [];
      for await (const chunk of await download.createReadStream()) chunks.push(chunk);
      assert.deepEqual(Buffer.concat(chunks), expected); if (filename) assert.equal(download.suggestedFilename(), filename);
    };
    await page.goto(`${origin}/local-runs/${id}`);
    await page.getByRole('heading', { name: work.request.document.knowledge_gaps[0].text }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Download workspace', exact: true }).isDisabled(), true);
    work.state = 'ready'; await page.reload();
    await page.getByText('Reused existing account · original authorship retained').waitFor();
    assert.equal(await page.locator('a[href*="candidate-account"]').count(), 0);
    const manual = page.locator('details.local-manual-setup'); assert.equal(await manual.getAttribute('open'), null);
    assert.equal(await page.getByRole('radio', { name: 'Codex', exact: true }).isChecked(), true);
    await saveDownload(page.getByRole('button', { name: 'Download workspace', exact: true }), fixtureZip, `reveal-${id}.zip`);
    await page.getByText('python3 start.py codex', { exact: true }).waitFor();
    assert.equal(calls.some(call => call.path.endsWith('/grants') || call.method === 'DELETE'), false);
    await page.getByRole('radio', { name: 'Claude Code', exact: true }).check();
    await saveDownload(page.getByRole('button', { name: 'Download workspace', exact: true }), fixtureZip);
    await page.getByText('python3 start.py claude', { exact: true }).waitFor();
    if (process.env.LOCAL_WORK_SCREENSHOTS) {
      const output = resolve(process.env.LOCAL_WORK_SCREENSHOTS); await mkdir(output, { recursive: true });
      await page.screenshot({ path: resolve(output, `setup-${new URL(origin).port}-desktop.png`), fullPage: true });
      await page.setViewportSize({ width: 390, height: 950 });
      await page.getByRole('radio', { name: 'Codex', exact: true }).check();
      await page.getByRole('radio', { name: 'Claude Code', exact: true }).check();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile setup fits the viewport');
      await page.screenshot({ path: resolve(output, `setup-${new URL(origin).port}-mobile.png`), fullPage: true });
      await page.setViewportSize({ width: 1280, height: 1000 });
    }
    assert.deepEqual(calls.filter(call => call.path.endsWith('/setup-kit')).map(call => call.body.client), ['codex', 'claude_code']);
    await page.waitForFunction(() => window.downloadUrls.created.length === window.downloadUrls.released.length);
    await page.reload(); await page.getByRole('radio', { name: 'Claude Code', exact: true }).waitFor();
    assert.equal(await page.getByRole('radio', { name: 'Claude Code', exact: true }).isChecked(), true);
    assert.equal(await page.getByRole('button', { name: 'Download new connection', exact: true }).count(), 0);
    assert.equal(work.grants[0].revoked_at, null, 'Downloads preserve existing connections');
    setupFailure = true;
    await page.getByRole('button', { name: 'Download workspace', exact: true }).click();
    await page.getByText('Workspace preparation unavailable. Try again.', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Download workspace', exact: true }).isEnabled(), true);
    const beforeAbort = downloads.length; holdSetup = true;
    await page.getByRole('button', { name: 'Download workspace', exact: true }).click();
    await page.getByRole('button', { name: 'Preparing workspace…', exact: true }).waitFor();
    await page.evaluate(() => window.dispatchEvent(new Event('pagehide'))); releaseSetup?.();
    await page.getByRole('button', { name: 'Download workspace', exact: true }).waitFor();
    assert.equal(downloads.length, beforeAbort, 'Navigation cancellation never saves a late workspace archive');
    await manual.locator('summary').click();
    await page.getByRole('radio', { name: 'Codex', exact: true }).check();
    await page.getByText('python3 start.py codex --login', { exact: true }).waitFor();
    await page.getByText('python3 start.py codex --logout', { exact: true }).waitFor();
    await page.getByRole('radio', { name: 'Claude Code', exact: true }).check();
    await page.getByText('python3 start.py claude --check-only', { exact: true }).waitFor();
    assert.equal(await page.locator('#local-credential').count(), 0);
    assert.equal(await page.getByRole('button', { name: /Create.*connection/ }).count(), 0);
    assert.equal(calls.some(call => call.path.endsWith('/grants')), false, 'Anonymous setup never creates a grant');
    assert.doesNotMatch(await manual.innerText(), /REVEAL_MCP_TOKEN|Bearer|30 minutes/);
    assert.deepEqual(await page.evaluate(() => ({ ...localStorage })), { 'reveal.local-agent-client': 'claude_code' });
    assert.equal(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }).includes('rvls_')), false);
    await page.getByText('Frozen inputs and package identity', { exact: true }).click();
    await saveDownload(page.getByRole('button', { name: 'Download research seed and manifest', exact: true }), Buffer.from(JSON.stringify({ package: {}, manifest: {}, artifacts: [] }, null, 2)));
    denied = true; await page.getByRole('button', { name: 'Revoke', exact: true }).last().click();
    await page.getByText('Access revoked.').waitFor(); assert.equal(await page.locator('#local-credential').count(), 0);
    denied = false; await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('button', { name: 'Close local research', exact: true }).click(); await page.getByRole('button', { name: 'Close local research', exact: true }).click();
    await page.getByText('Local research closed. Submitted findings and accepted accounts are retained.').waitFor();
    assert.equal(await page.getByRole('button', { name: 'Download workspace', exact: true }).isDisabled(), true);
    assert.equal(calls.some(call => call.path.endsWith('/jobs')), false, 'Setup and local results never start hosted jobs');
    assert.equal(errors.length, 0, errors.join('\n')); assert.deepEqual(unexpected, []);
    console.log(`${origin}: both anonymous ZIP clients, preference, URL cleanup, setup failure/abort, explicit login/logout instructions, existing grant revocation, validation-only links and close passed; zero grants or hosted jobs.`);
    await context.close();
  }
} finally { await browser.close(); }
