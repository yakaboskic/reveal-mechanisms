import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { frames, openReport, options, run } from './check-flow.mjs';

const owner = randomUUID(), jobId = randomUUID(), gapId = 'dapper:KnowledgeGap.' + 'G'.repeat(32);
const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json', ...headers } });
const sse = events => new Response(events.map(event => `id: ${event.id}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } });
const event = (id, status) => ({ id, job_id: jobId, event_type: status === 'succeeded' ? 'result' : 'progress', status, stage: status === 'succeeded' ? 'complete' : 'authoring_account' });

async function temporary(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'reveal-client-check-'));
  await fs.chmod(directory, 0o700);
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

function backend({ loseJobResponse = false, splitEvents = false, mismatchOwner = false } = {}) {
  const calls = [], mutations = new Map();
  let createdJobs = 0, eventCalls = 0, lost = false;
  const job = { id: jobId, kind: 'analysis', owner_user_id: owner, status: 'succeeded', result: { kind: 'analysis', account_ids: ['dapper:ScientificAccount.' + 'A'.repeat(32)], paragraph_job_ids: [] } };
  async function fetcher(input, init = {}) {
    const url = new URL(input), route = url.pathname, body = init.body ? JSON.parse(init.body) : null;
    calls.push({ route, init, body });
    assert.equal(init.redirect, 'manual');
    if (init.method !== 'GET') assert.equal(init.headers.Origin, url.origin);
    if (route === '/api/session') return json({ principal: { user_id: owner, principal_kind: 'registered', workspace_expires_at: null } }, 200, { 'Set-Cookie': 'reveal-session=private-cookie; HttpOnly; Path=/' });
    assert.equal(init.headers.Cookie, 'reveal-session=private-cookie');
    if (route === '/api/backend/v1/me') return json({ user_id: mismatchOwner ? randomUUID() : owner });
    if (route.endsWith('/knowledge-gaps/search')) return json({ items: [{ gap: { object: { id: gapId }, source: { source_id: 'dismech:test', source_revision: 'a'.repeat(64) } } }] });
    if (route.endsWith('/mechanisms/suggest')) return json({ suggestion_id: randomUUID(), automatic_anchors: [{ factor: { source: 'eaggl', source_id: 'factor:test', source_revision: 'b'.repeat(64), object: { id: 'dapper:Mechanism.' + 'M'.repeat(32) } } }] });
    if (route.endsWith('/events')) {
      eventCalls += 1;
      if (splitEvents && eventCalls === 1) return sse([event('1', 'running')]);
      if (splitEvents) assert.equal(init.headers['Last-Event-ID'], '1');
      return sse([event('1', 'running'), event('2', 'succeeded')]);
    }
    if (route.includes('/accounts/')) return json({ artifacts: [] });
    if (route === `/api/backend/v1/jobs/${jobId}`) return json(job);
    const key = init.headers['Idempotency-Key'];
    assert.ok(key, `Missing mutation key for ${route}`);
    if (mutations.has(key)) {
      const prior = mutations.get(key);
      assert.deepEqual(body, prior.body);
      return json(prior.result, prior.status);
    }
    let result, status = 200;
    if (route === '/api/backend/v1/drafts') { status = 201; result = { id: randomUUID(), version: 1, composer: body.composer }; }
    else if (route.includes('/drafts/') && init.method === 'PATCH') result = { id: route.split('/').at(-1), version: body.expected_version + 1 };
    else if (route.includes('/drafts/') && init.method === 'DELETE') result = { deleted: true };
    else if (route === '/api/backend/v1/jobs') { createdJobs += 1; status = 202; result = { id: jobId, status: 'queued' }; }
    else throw new Error(`Unexpected test request: ${route}`);
    mutations.set(key, { body, result, status });
    if (route === '/api/backend/v1/jobs' && loseJobResponse && !lost) { lost = true; throw new Error('Lost response after commit'); }
    return json(result, status);
  }
  return { fetcher, calls, createdJobs: () => createdJobs };
}

test('default smoke uses cookie/origin and deletes the draft without creating jobs', async t => {
  const directory = await temporary(t), config = options(['--report', path.join(directory, 'smoke.json')]);
  const mock = backend(), report = await run(config, { fetcher: mock.fetcher });
  assert.equal(report.passed, true);
  assert.equal(report.checks.temporary_draft_removed, true);
  assert.equal(mock.createdJobs(), 0);
  assert.equal(mock.calls.some(call => call.route === '/api/backend/v1/jobs'), false);
  assert.equal((await fs.stat(config.report)).mode & 0o777, 0o600);
  assert.equal((await fs.readFile(config.report, 'utf8')).includes('private-cookie'), false);
  const before = mock.calls.filter(call => call.init.method !== 'GET').length;
  await run(config, { fetcher: mock.fetcher });
  assert.equal(mock.calls.filter(call => call.init.method !== 'GET').length, before + 1); // Session renewal only.
});

test('lost job acknowledgement resumes same body/key and commits only one job', async t => {
  const directory = await temporary(t), config = options(['--run-analysis', '--report', path.join(directory, 'analysis.json')]);
  const mock = backend({ loseJobResponse: true, splitEvents: true });
  await assert.rejects(run(config, { fetcher: mock.fetcher, sleep: async () => {} }));
  const pending = JSON.parse(await fs.readFile(config.report, 'utf8')).operations.create_job;
  assert.ok(pending.key && !pending.result);
  assert.deepEqual(pending.body.budgets, { max_accounts: 1 });
  const report = await run(config, { fetcher: mock.fetcher, sleep: async () => {} });
  assert.equal(report.passed, true);
  assert.equal(mock.createdJobs(), 1);
  const attempts = mock.calls.filter(call => call.route === '/api/backend/v1/jobs');
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].init.headers['Idempotency-Key'], attempts[1].init.headers['Idempotency-Key']);
  assert.deepEqual(attempts[0].body, attempts[1].body);
  assert.equal(report.jobs[jobId].events, 2); // Duplicate replay was ignored.
  assert.equal(report.jobs[jobId].cursor, '2');
  assert.equal(mock.calls.filter(call => call.route === `/api/backend/v1/jobs/${jobId}`).length, 1); // Terminal fetch only.
});

test('following an existing UI job never prepares a draft or submits any job', async t => {
  const directory = await temporary(t), config = options(['--job-id', jobId, '--report', path.join(directory, 'follow.json')]);
  const mock = backend(), report = await run(config, { fetcher: mock.fetcher });
  assert.equal(report.passed, true);
  assert.equal(mock.createdJobs(), 0);
  assert.equal(mock.calls.some(call => call.route.includes('/drafts') || call.route.includes('/mechanisms/')), false);
  assert.deepEqual(Object.keys(report.operations), []);
});

test('SSE handles split CRLF, comments and multiline data', async () => {
  const encoder = new TextEncoder();
  const chunks = [': heartbeat\r', '\nid: 3\r\nevent: progress\r\ndata: first\r\n', 'data: second\r', '\n\r\n'];
  const stream = new ReadableStream({ start(controller) { for (const chunk of chunks) controller.enqueue(encoder.encode(chunk)); controller.close(); } });
  const result = []; for await (const frame of frames(stream)) result.push(frame);
  assert.deepEqual(result, [{ id: '3', event: 'progress', data: 'first\nsecond' }]);
});

test('report lock refuses concurrent checker and origin/mode changes', async t => {
  const directory = await temporary(t), report = path.join(directory, 'locked.json');
  const config = options(['--report', report]), state = await openReport(config);
  await assert.rejects(openReport(config), /locked/);
  await state.close();
  await assert.rejects(openReport(options(['--report', report, '--run-analysis'])), /another origin or mode/);
  await assert.rejects(openReport(options(['--report', report, '--base-url', 'http://localhost:3201'])), /another origin or mode/);
});

test('identity mismatch fails before scientific writes and no arbitrary error text enters report', async t => {
  const directory = await temporary(t), config = options(['--report', path.join(directory, 'identity.json')]);
  const mock = backend({ mismatchOwner: true });
  await assert.rejects(run(config, { fetcher: mock.fetcher }), /identities disagree/);
  assert.equal(mock.createdJobs(), 0);
  assert.equal(mock.calls.length, 2);
});

test('paid creation and existing-job following cannot accidentally be combined', () => {
  assert.throws(() => options(['--run-analysis', '--job-id', jobId]), /never both/);
  assert.equal(options([]).runAnalysis, false);
  assert.throws(() => options(['--base-url', 'http://remote.example']), /loopback/);
});

test('gateway control frames reconnect without changing a job cursor or polling status', async t => {
  const directory = await temporary(t), config = options(['--job-id', jobId, '--report', path.join(directory, 'control.json')]);
  const mock = backend(); let connections = 0;
  const fetcher = async (input, init) => {
    if (new URL(input).pathname.endsWith('/events') && ++connections === 1) {
      return new Response('event: ready\ndata: {}\n\nevent: connection_degraded\ndata: {}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
    }
    return mock.fetcher(input, init);
  };
  const report = await run(config, { fetcher, sleep: async () => {} });
  assert.equal(report.passed, true);
  assert.equal(report.jobs[jobId].events, 2);
  assert.equal(mock.calls.find(call => call.route.endsWith('/events')).init.headers['Last-Event-ID'], '0');
  assert.equal(mock.calls.filter(call => call.route === `/api/backend/v1/jobs/${jobId}`).length, 1);
});

function acceptedFlow({ corruptArtifact = false } = {}) {
  const mock = backend(), calls = [], accountId = 'dapper:ScientificAccount.' + 'A'.repeat(32);
  const paragraphJobId = randomUUID(), paragraphId = 'dapper:Paragraph.' + 'P'.repeat(32);
  const bytes = Buffer.from('Retained source evidence\n'), sha = createHash('sha256').update(bytes).digest('hex');
  const download = `https://artifacts.example.invalid/qa/source?X-Amz-Signature=test-capability`;
  async function fetcher(input, init) {
    const url = new URL(input), route = url.pathname;
    calls.push({ url: url.href, init });
    if (url.origin === 'https://artifacts.example.invalid') {
      // Cross-origin artifact download must never carry gateway/session credentials.
      const headers = new Headers(init.headers);
      assert.equal(headers.get('Cookie'), null);
      assert.equal(headers.get('Authorization'), null);
      assert.equal(init.redirect, 'error');
      assert.equal(url.href, download);
      return new Response(corruptArtifact ? Buffer.from('changed evidence') : bytes);
    }
    if (route !== '/api/session') assert.equal(init.headers.Cookie, 'reveal-session=private-cookie');
    if (route === `/api/backend/v1/jobs/${jobId}`) return json({ id: jobId, kind: 'analysis', status: 'succeeded', owner_user_id: owner,
      result: { kind: 'analysis', account_ids: [accountId], paragraph_job_ids: [paragraphJobId] } });
    if (route === `/api/backend/v1/accounts/${encodeURIComponent(accountId)}`) return json({ root: { id: accountId },
      artifacts: [{ file: { sha256: sha }, availability: 'available' }] });
    if (route === `/api/backend/v1/artifacts/${sha}`) return new Response(null, { status: 307, headers: { Location: download } });
    if (route === `/api/backend/v1/jobs/${paragraphJobId}/events`) return sse([
      { ...event('1', 'running'), job_id: paragraphJobId }, { ...event('2', 'succeeded'), job_id: paragraphJobId }]);
    if (route === `/api/backend/v1/jobs/${paragraphJobId}`) return json({ id: paragraphJobId, kind: 'paragraph', status: 'succeeded', owner_user_id: owner,
      result: { kind: 'paragraph', account_id: accountId, paragraph_id: paragraphId } });
    if (route === `/api/backend/v1/paragraphs/${encodeURIComponent(paragraphId)}`) return json({ root: { id: paragraphId } });
    if (route === `/api/backend/v1/paragraphs/${encodeURIComponent(paragraphId)}/export`) {
      assert.equal(url.searchParams.get('format'), 'markdown');
      return new Response('A cited research statement. [1]\n', { headers: { 'Content-Type': 'text/markdown' } });
    }
    return mock.fetcher(input, init);
  }
  return { fetcher, calls, accountId, paragraphJobId, paragraphId, sha, bytes, mock };
}

test('accepted account follows its automatic paragraph, exports text and verifies a credential-free artifact download', async t => {
  const directory = await temporary(t), config = options(['--job-id', jobId, '--report', path.join(directory, 'accepted.json')]);
  const server = acceptedFlow(), report = await run(config, { fetcher: server.fetcher });
  assert.equal(report.passed, true);
  assert.equal(report.checks.automatic_paragraphs_succeeded, true);
  assert.deepEqual(report.results.accounts, [server.accountId]);
  assert.deepEqual(report.results.paragraphs, [server.paragraphId]);
  assert.deepEqual(report.results.artifacts, [{ sha256: server.sha, bytes: server.bytes.length }]);
  assert.equal(report.jobs[server.paragraphJobId].terminal_status, 'succeeded');
  assert.equal(report.jobs[server.paragraphJobId].events, 2);
  assert.equal(server.calls.filter(call => call.url.includes('/export?format=markdown')).length, 1);
  assert.equal(server.calls.filter(call => call.url.startsWith('https://artifacts.example.invalid')).length, 1);
  assert.equal(server.calls.some(call => new URL(call.url).pathname === '/api/backend/v1/jobs' && call.init.method === 'POST'), false);
  assert.equal(server.mock.createdJobs(), 0);
  const saved = await fs.readFile(config.report, 'utf8');
  assert.equal(saved.includes('test-capability'), false);
  assert.equal(saved.includes('private-cookie'), false);
});

test('artifact checksum mismatch fails the report without submitting another job', async t => {
  const directory = await temporary(t), config = options(['--job-id', jobId, '--report', path.join(directory, 'corrupt.json')]);
  const server = acceptedFlow({ corruptArtifact: true });
  await assert.rejects(run(config, { fetcher: server.fetcher }), /checksum did not match/);
  const report = JSON.parse(await fs.readFile(config.report, 'utf8'));
  assert.notEqual(report.passed, true);
  assert.equal(report.completed_at, undefined);
  assert.equal(report.jobs[jobId].terminal_status, 'succeeded'); // Scientific job is preserved, not relaunched.
  assert.deepEqual(report.results.artifacts, []);
  assert.equal(server.mock.createdJobs(), 0);
});

test('failed scientific job remains a failed result instead of a passing acceptance report', async t => {
  const directory = await temporary(t), config = options(['--job-id', jobId, '--report', path.join(directory, 'failed.json')]);
  const mock = backend(), calls = [];
  const fetcher = async (input, init) => {
    const route = new URL(input).pathname; calls.push({ route, init });
    if (route.endsWith('/events')) return sse([{ ...event('1', 'failed'), event_type: 'failure' }]);
    if (route === `/api/backend/v1/jobs/${jobId}`) return json({ id: jobId, kind: 'analysis', status: 'failed', owner_user_id: owner,
      result: null, failure: { code: 'REVIEW_REJECTED', message: 'Scientific review did not accept the captured result.' } });
    return mock.fetcher(input, init);
  };
  const report = await run(config, { fetcher });
  assert.equal(report.passed, false);
  assert.equal(report.checks.job_sse_terminal, true);
  assert.equal(report.checks.scientific_success, false);
  assert.equal(report.jobs[jobId].job.failure.code, 'REVIEW_REJECTED');
  assert.ok(report.completed_at);
  assert.equal(calls.filter(call => call.route === `/api/backend/v1/jobs/${jobId}`).length, 1);
  assert.equal(calls.some(call => call.route.includes('/accounts/') || call.route.includes('/paragraphs/') || call.route.endsWith('/retry-review')), false);
  assert.equal(mock.createdJobs(), 0);
  const resumed = await run(config, { fetcher });
  assert.equal(resumed.passed, false);
  assert.equal(mock.createdJobs(), 0);
});
