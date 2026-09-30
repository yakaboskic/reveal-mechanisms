#!/usr/bin/env node
/** Independent HTTP acceptance through the local client gateway. No status polling. */
import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const TERMINAL = new Set(['succeeded', 'insufficient_evidence', 'failed', 'cancelled']);
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
export class FlowError extends Error {}
class HttpError extends FlowError {
  constructor(status, code = '') { super(`HTTP ${status}${/^[A-Z_]{1,80}$/.test(code) ? ` (${code})` : ''}`); this.status = status; }
}
function requireThat(ok, message) { if (!ok) throw new FlowError(message); }

export function options(argv) {
  const value = { baseUrl: 'http://localhost:3200', query: 'coronary artery disease', runAnalysis: false, timeoutSeconds: 3600 };
  const names = { '--base-url': 'baseUrl', '--report': 'report', '--query': 'query', '--job-id': 'jobId', '--timeout-seconds': 'timeoutSeconds' };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--run-analysis') value.runAnalysis = true;
    else if (names[argv[i]] && argv[i + 1] && !argv[i + 1].startsWith('--')) value[names[argv[i]]] = argv[++i];
    else throw new FlowError('Unknown or incomplete option. See README.md for usage.');
  }
  const base = new URL(value.baseUrl);
  requireThat(['http:', 'https:'].includes(base.protocol) && !base.username && !base.password && !base.search && !base.hash && base.pathname === '/', 'Use the client origin as --base-url.');
  requireThat(base.protocol === 'https:' || ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname), 'HTTP checks require loopback.');
  value.baseUrl = base.origin;
  requireThat(!value.jobId || UUID.test(value.jobId), 'Invalid existing job ID.');
  requireThat(!(value.jobId && value.runAnalysis), 'Choose either --job-id or --run-analysis; never both.');
  value.timeoutSeconds = Number(value.timeoutSeconds);
  requireThat(Number.isInteger(value.timeoutSeconds) && value.timeoutSeconds >= 1 && value.timeoutSeconds <= 28800, 'Timeout must be 1–28800 seconds.');
  value.mode = value.jobId ? 'follow' : value.runAnalysis ? 'analysis' : 'smoke';
  value.report ||= `.runtime/check-flow-${value.jobId || value.mode}.json`;
  return value;
}

async function readPrivate(file) {
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await handle.stat();
    requireThat(stat.isFile() && !(stat.mode & 0o077) && stat.uid === process.getuid(), 'Report must be a private regular file.');
    return JSON.parse(await handle.readFile('utf8'));
  } finally { await handle.close(); }
}

export async function openReport(config) {
  requireThat(typeof process.getuid === 'function', 'The private-report checker requires macOS, Linux or WSL file permissions.');
  const file = path.resolve(config.report), directory = path.dirname(file);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  requireThat(stat.isDirectory() && !stat.isSymbolicLink() && !(stat.mode & 0o077) && stat.uid === process.getuid(), 'Report directory must be private (0700).');
  const lockPath = `${file}.lock`;
  let lock;
  try { lock = await fs.open(lockPath, 'wx', 0o600); }
  catch { throw new FlowError('Report is locked. Stop its other checker process before recovering an abandoned .lock file.'); }
  await lock.writeFile(JSON.stringify({ pid: process.pid }));
  let report;
  try {
    try { report = await readPrivate(file); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    report ||= { format: 'reveal.client-flow/1', base_url: config.baseUrl, mode: config.mode, query: config.query,
      job_id: config.jobId || null, started_at: new Date().toISOString(), operations: {}, checks: {}, jobs: {} };
    requireThat(report.format === 'reveal.client-flow/1' && report.base_url === config.baseUrl && report.mode === config.mode,
      'Report belongs to another origin or mode; use the original arguments.');
    requireThat(!config.jobId || report.job_id === config.jobId, 'Report belongs to another existing job.');
    const save = async () => {
      const temporary = path.join(directory, `.flow-${randomUUID()}.tmp`);
      const handle = await fs.open(temporary, 'wx', 0o600);
      try { await handle.writeFile(JSON.stringify(report, null, 2) + '\n'); await handle.sync(); }
      finally { await handle.close(); }
      try { await fs.rename(temporary, file); }
      finally { await fs.unlink(temporary).catch(() => {}); }
    };
    await save();
    return { report, save, close: async () => { await lock.close(); await fs.unlink(lockPath); } };
  } catch (error) { await lock.close(); await fs.unlink(lockPath); throw error; }
}

export class Client {
  constructor(base, fetcher = fetch) { this.base = base; this.fetcher = fetcher; this.cookies = new Map(); }
  async request(route, { method = 'GET', body, key, headers = {}, signal, expected = 200 } = {}) {
    requireThat(route.startsWith('/api/') && !route.startsWith('//'), 'Checker route must stay on the client origin.');
    const requestHeaders = { Accept: 'application/json', ...headers };
    if (this.cookies.size) requestHeaders.Cookie = [...this.cookies].map(([name, value]) => `${name}=${value}`).join('; ');
    if (method !== 'GET') requestHeaders.Origin = this.base;
    if (body !== undefined) requestHeaders['Content-Type'] = 'application/json';
    if (key) requestHeaders['Idempotency-Key'] = key;
    const response = await this.fetcher(this.base + route, { method, headers: requestHeaders, redirect: 'manual',
      signal: signal || AbortSignal.timeout(90000), ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
    for (const cookie of response.headers.getSetCookie()) {
      const pair = cookie.split(';', 1)[0], equals = pair.indexOf('=');
      if (equals > 0) this.cookies.set(pair.slice(0, equals), pair.slice(equals + 1));
    }
    if (!(Array.isArray(expected) ? expected.includes(response.status) : response.status === expected)) {
      let code = ''; try { code = (await response.json()).code; } catch {}
      throw new HttpError(response.status, code);
    }
    return response;
  }
  async json(route, args) { return (await this.request(route, args)).json(); }
}

/** Parse SSE incrementally, including split CRLF, comments, and multiline data. */
export async function* frames(body) {
  const reader = body.getReader(), decoder = new TextDecoder();
  let buffer = '', event = { data: [] };
  try {
    while (true) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      let match;
      while ((match = /\r\n|\r|\n/.exec(buffer))) {
        if (!next.done && match[0] === '\r' && match.index === buffer.length - 1) break;
        const line = buffer.slice(0, match.index); buffer = buffer.slice(match.index + match[0].length);
        if (!line) { if (event.data.length) yield { ...event, data: event.data.join('\n') }; event = { data: [] }; }
        else if (!line.startsWith(':')) {
          const colon = line.indexOf(':'), field = colon < 0 ? line : line.slice(0, colon);
          const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
          if (field === 'data') event.data.push(value);
          else if (field === 'id' && !value.includes('\0')) event.id = value;
          else if (field === 'event') event.event = value;
        }
      }
      if (next.done) return;
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export async function mutation(client, state, name, route, method, body, expected) {
  const prior = state.report.operations[name];
  if (!prior) {
    state.report.operations[name] = { route, method, body, expected, key: randomUUID() };
    await state.save(); // Commit intent before the potentially ambiguous request.
  } else requireThat(prior.route === route && prior.method === method && JSON.stringify(prior.body) === JSON.stringify(body), 'Saved mutation differs; refusing a new operation.');
  const operation = state.report.operations[name];
  if (!operation.result) {
    operation.result = await client.json(operation.route, { method: operation.method, body: operation.body, key: operation.key, expected: operation.expected });
    await state.save();
  }
  return operation.result;
}

async function session(client, state) {
  const value = await client.json('/api/session', { method: 'POST', body: {} });
  requireThat(value.principal?.user_id && value.principal.principal_kind === 'registered', 'Demo gateway must return a registered principal.');
  requireThat(!state.report.owner_user_id || state.report.owner_user_id === value.principal.user_id, 'Demo identity changed; refusing to resume another user’s report.');
  state.report.owner_user_id = value.principal.user_id;
  const me = await client.json('/api/backend/v1/me');
  requireThat(me.user_id === value.principal.user_id, 'Gateway and backend identities disagree.');
  state.report.checks.session_and_me = true;
  await state.save();
}

async function prepareDraft(client, state) {
  const report = state.report;
  if (!report.composer) {
    const search = await client.json('/api/backend/v1/knowledge-gaps/search?' + new URLSearchParams({ q: report.query, mode: 'lexical', limit: '20' }));
    const hits = search.items || [];
    const gap = (hits.find(item => item.gap?.source?.source_id === 'dismech:disorders/Coronary_Artery_Disease#discussion:cad_pgsxc_reverse_causation') || hits[0])?.gap;
    requireThat(gap?.object?.id && gap?.source?.source_revision, 'Catalog query returned no usable source gap.');
    const selected = { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision };
    const suggestions = await client.json('/api/backend/v1/mechanisms/suggest', { method: 'POST', body: {
      source_gap: selected, manual_eaggl_anchors: [], dismissed_source_ids: [], subquery: '', mode: 'semantic', model: 'cfde-inc-v2' } });
    const factor = suggestions.automatic_anchors?.[0]?.factor;
    requireThat(factor?.source_id && factor.object?.id, 'No current mapped anchor was returned.');
    report.composer = { source_gap: selected, eaggl_anchors: [{ reference: { source: factor.source, source_id: factor.source_id,
      source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'automatic', suggestion_id: suggestions.suggestion_id }],
      dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg'] };
    report.checks.catalog_and_stored_suggestions = true; await state.save();
  }
  const draft = await mutation(client, state, 'create_draft', '/api/backend/v1/drafts', 'POST',
    { name: 'Standalone client flow check', composer: report.composer }, 201);
  if (!report.checks.draft_idempotency) {
    const op = report.operations.create_draft;
    const replay = await client.json(op.route, { method: op.method, body: op.body, key: op.key, expected: 201 });
    requireThat(replay.id === draft.id && replay.version === draft.version, 'Draft retry changed its identity or version.');
    report.checks.draft_idempotency = true; await state.save();
  }
  const edited = await mutation(client, state, 'edit_draft', `/api/backend/v1/drafts/${encodeURIComponent(draft.id)}`, 'PATCH',
    { expected_version: draft.version, name: 'Standalone client flow check edited' }, 200);
  requireThat(edited.version === draft.version + 1, 'Draft edit did not advance exactly one version.');
  report.checks.versioned_draft_edit = true; await state.save();
  return edited;
}

export async function followJob(client, state, id, config, sleep = pause) {
  const record = state.report.jobs[id] ||= { cursor: '0', events: 0 };
  if (!record.terminal_status) {
    const deadline = Date.now() + config.timeoutSeconds * 1000;
    let renewals = 0, failures = 0;
    while (!record.terminal_status && Date.now() < deadline) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), Math.min(245000, deadline - Date.now()));
      try {
        const response = await client.request(`/api/backend/v1/jobs/${encodeURIComponent(id)}/events`, {
          headers: { Accept: 'text/event-stream', 'Last-Event-ID': record.cursor }, signal: controller.signal });
        requireThat(response.headers.get('content-type')?.startsWith('text/event-stream'), 'Gateway did not preserve SSE content type.');
        for await (const frame of frames(response.body)) {
          // Current job streams contain only JobEvents/comments. A gateway may
          // relay these control frames; they never advance the numeric cursor.
          if (frame.event === 'ready') continue;
          if (frame.event === 'connection_degraded') break;
          if (frame.event === 'access_revoked') throw new HttpError(401);
          if (['reset', 'resync_required'].includes(frame.event)) throw new FlowError('Job stream requested a snapshot reset; inspect this report before resuming.');
          const event = JSON.parse(frame.data), cursor = frame.id || event.id;
          requireThat(event.job_id === id && /^[0-9]+$/.test(cursor), 'SSE event has the wrong job or cursor.');
          requireThat(!frame.id || !event.id || frame.id === event.id, 'SSE frame and event cursors disagree.');
          if (BigInt(cursor) <= BigInt(record.cursor)) continue;
          record.cursor = cursor; record.events += 1;
          record.last_event = { status: event.status, stage: event.stage, event_type: event.event_type };
          if (TERMINAL.has(event.status)) record.terminal_status = event.status;
          await state.save();
          if (record.terminal_status) break;
        }
        failures = 0;
      } catch (error) {
        if (error instanceof HttpError && error.status === 401 && renewals++ < 1) await session(client, state);
        else if ((error instanceof HttpError && ![502, 503, 504].includes(error.status)) || error instanceof FlowError && !(error instanceof HttpError)) throw error;
        else if (++failures > 8) throw new FlowError('Repeated SSE connection failures; rerun with this report to resume.');
      } finally { clearTimeout(timer); }
      if (!record.terminal_status) {
        record.reconnections = (record.reconnections || 0) + 1; await state.save();
        await sleep(Math.min(5000, 250 * 2 ** Math.min(failures, 4)));
      }
    }
    requireThat(record.terminal_status, 'SSE wait timed out. The job was not cancelled; resume with this report.');
  }
  // One authoritative fetch after terminal SSE, never a timer-driven status loop.
  const job = await client.json(`/api/backend/v1/jobs/${encodeURIComponent(id)}`);
  requireThat(job.owner_user_id === state.report.owner_user_id && TERMINAL.has(job.status), 'Terminal job owner/status verification failed.');
  record.job = job; await state.save(); return job;
}

async function verifyArtifact(client, artifact) {
  const sha = artifact.file?.sha256;
  if (artifact.availability !== 'available' || !/^[a-f0-9]{64}$/.test(sha || '')) return null;
  let response = await client.request(`/api/backend/v1/artifacts/${sha}`, { expected: [200, 307] });
  if (response.status === 307) {
    const target = new URL(response.headers.get('location'));
    requireThat(target.protocol === 'https:' && !target.username && !target.password, 'Artifact redirect is invalid.');
    // This external download carries neither the client cookie nor backend bearer.
    response = await client.fetcher(target, { redirect: 'error', signal: AbortSignal.timeout(90000) });
  }
  requireThat(response.ok && response.body, 'Artifact download failed.');
  const hash = createHash('sha256'); let bytes = 0;
  for await (const chunk of response.body) { bytes += chunk.length; requireThat(bytes <= 40 * 1024 * 1024, 'Artifact exceeded checker download bound.'); hash.update(chunk); }
  requireThat(hash.digest('hex') === sha, 'Downloaded artifact checksum did not match.');
  return { sha256: sha, bytes };
}

async function inspectResults(client, state, job, config, sleep) {
  const report = state.report;
  report.results ||= { accounts: [], paragraphs: [], artifacts: [] };
  if (job.status === 'insufficient_evidence') {
    const result = await client.json(`/api/backend/v1/analysis-outcomes/${encodeURIComponent(job.result.outcome_id)}`);
    requireThat(result.outcome === 'insufficient_evidence', 'Unexpected analysis outcome.');
    report.results.outcome_id = result.id;
  } else if (job.status === 'succeeded') {
    for (const id of job.result.account_ids || []) {
      const account = await client.json(`/api/backend/v1/accounts/${encodeURIComponent(id)}`);
      if (!report.results.accounts.includes(id)) report.results.accounts.push(id);
      const available = (account.artifacts || []).find(item => item.availability === 'available');
      if (available && !report.results.artifacts.some(item => item.sha256 === available.file?.sha256)) {
        const artifact = await verifyArtifact(client, available); if (artifact) report.results.artifacts.push(artifact);
      }
      await state.save();
    }
    if (job.result.paragraph_job_ids?.length) report.checks.automatic_paragraphs_succeeded = true;
    for (const id of job.result.paragraph_job_ids || []) {
      const paragraph = await followJob(client, state, id, config, sleep);
      if (paragraph.status === 'succeeded') {
        const paragraphId = paragraph.result.paragraph_id;
        await client.json(`/api/backend/v1/paragraphs/${encodeURIComponent(paragraphId)}`);
        await client.request(`/api/backend/v1/paragraphs/${encodeURIComponent(paragraphId)}/export?format=markdown`);
        if (!report.results.paragraphs.includes(paragraphId)) report.results.paragraphs.push(paragraphId);
      } else report.checks.automatic_paragraphs_succeeded = false;
      await state.save();
    }
  }
  report.checks.job_sse_terminal = true;
  report.checks.scientific_success = ['succeeded', 'insufficient_evidence'].includes(job.status);
  await state.save();
}

export async function run(config, { fetcher = fetch, sleep = pause } = {}) {
  const state = await openReport(config), client = new Client(config.baseUrl, fetcher);
  try {
    await session(client, state);
    if (state.report.completed_at) return state.report;
    if (config.mode !== 'follow') {
      const draft = await prepareDraft(client, state);
      if (config.mode === 'smoke') {
        await mutation(client, state, 'delete_draft', `/api/backend/v1/drafts/${encodeURIComponent(draft.id)}`, 'DELETE', { expected_version: draft.version }, 200);
        state.report.checks.temporary_draft_removed = true;
      } else {
        requireThat(config.runAnalysis, 'Analysis creation requires --run-analysis.');
        const job = await mutation(client, state, 'create_job', '/api/backend/v1/jobs', 'POST', {
          kind: 'analysis', draft_id: draft.id, draft_version: draft.version, budgets: { max_accounts: 1 } }, 202);
        state.report.job_id = job.id; await state.save();
      }
    }
    if (state.report.job_id) {
      const job = await followJob(client, state, state.report.job_id, config, sleep);
      await inspectResults(client, state, job, config, sleep);
    }
    state.report.completed_at = new Date().toISOString();
    state.report.passed = Object.values(state.report.checks).every(value => value !== false);
    delete state.report.last_error; await state.save(); return state.report;
  } catch (error) {
    state.report.last_error = error instanceof FlowError ? error.message : 'Connection or response failure; resume with the same report.';
    await state.save(); throw error;
  } finally { await state.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const config = options(process.argv.slice(2)), report = await run(config);
    console.log(JSON.stringify({ passed: report.passed, mode: report.mode, checks: report.checks, report: path.resolve(config.report) }));
    if (!report.passed) process.exitCode = 1;
  } catch (error) {
    console.error(error instanceof FlowError ? error.message : 'Flow check failed; use the saved report to resume.');
    process.exitCode = 1;
  }
}
