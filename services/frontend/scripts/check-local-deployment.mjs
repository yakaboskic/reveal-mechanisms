#!/usr/bin/env node
/** Real local gateway/session smoke test; never submits scientific work. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const origin = new URL(process.env.DEPLOYMENT_BASE_URL || 'http://localhost:3000').origin;
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname));
const output = resolve(import.meta.dirname, '../../../.runtime/deployment');
await mkdir(output, { recursive: true });
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  const result = await page.evaluate(async () => {
    const session = await fetch('/api/session/anonymous', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }, body: '{}' });
    const me = await fetch('/api/backend/v1/me');
    return { session: session.status, me: me.status, kind: (await me.json()).principal_kind };
  });
  assert.ok([200, 201].includes(result.session), JSON.stringify(result));
  assert.equal(result.me, 200); assert.equal(result.kind, 'anonymous');
  await page.goto(origin + '/workspace', { waitUntil: 'networkidle' });
  const persistent = await page.evaluate(async () => (await fetch('/api/backend/v1/me')).status);
  assert.equal(persistent, 200, 'The browser must retain its session after navigation');
  assert.deepEqual(errors, []);
  const report = { anonymous_session: true, gateway_identity: true, session_survives_navigation: true,
    browser_errors: errors.length, scientific_execution: false };
  await writeFile(resolve(output, 'browser-verification.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
