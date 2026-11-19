"""The admin transcript read of one agent job, and the run view's configuration: readable turns from the captured
stream (deltas folded, tool calls paired, errors marked), paged, redacted, under the admin_content purpose with an
access record per read, and a plain answer for jobs with nothing captured."""
import gzip
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

from reveal_backend import app as api, jobs
from reveal_backend.admin_run import job_run
from reveal_backend.admin_transcript import MAX_TEXT, fold, job_transcript
from reveal_backend.box_stream import TRANSCRIPT_CLOCK, TRANSCRIPT_CUT
from reveal_backend.repository import Repository, now, uid
from test_admin_run import ADMIN, frozen

STREAM = 'attempt-{}/output/ledger/claude-stream.jsonl.gz'
STDERR = 'attempt-{}/output/ledger/claude-stderr.txt.gz'


def raw(*events):
    return b''.join(json.dumps(event).encode() + b'\n' for event in events)


def delta(index, kind, **value):
    return {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': index, 'delta': {'type': kind, **value}}}


def start(index, **block):
    return {'type': 'stream_event', 'event': {'type': 'content_block_start', 'index': index, 'content_block': block}}


def message(identity):
    return {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': identity}}}


def assistant(identity, *blocks):
    return {'type': 'assistant', 'message': {'id': identity, 'content': list(blocks)}}


def result(call_id, content, is_error=False):
    return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': content, 'is_error': is_error}]}}


INIT = {'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5', 'claude_code_version': '2.1.282', 'permissionMode': 'bypassPermissions',
        'tools': ['Read', 'Bash'], 'mcp_servers': [{'name': 'reveal', 'status': 'connected'}], 'session_id': 's', 'cwd': '/reveal/trusted'}
# One run as Claude Code streams it with --include-partial-messages: every message twice, as deltas and whole.
RUN = [{'type': TRANSCRIPT_CLOCK, 'at': '2026-11-19T10:00:00.000Z'}, INIT,
       message('m1'), start(0, type='thinking', thinking=''), delta(0, 'thinking_delta', thinking='Plan: '),
       delta(0, 'thinking_delta', thinking='fetch it.'), assistant('m1', {'type': 'thinking', 'thinking': 'Plan: fetch it.'}),
       start(1, type='tool_use', id='call-1', name='Bash', input={}), delta(1, 'input_json_delta', partial_json='{"command":'),
       delta(1, 'input_json_delta', partial_json='"curl -sSL x"}'),
       assistant('m1', {'type': 'tool_use', 'id': 'call-1', 'name': 'Bash', 'input': {'command': 'curl -sSL x'}}),
       {'type': 'rate_limit_event', 'rate_limit_info': {}},
       {'type': TRANSCRIPT_CLOCK, 'at': '2026-11-19T10:00:03.000Z'},
       result('call-1', 'curl: (6) Could not resolve host', is_error=True),
       message('m2'), start(0, type='tool_use', id='call-2', name='mcp__reveal__query_claims', input={}),
       assistant('m2', {'type': 'tool_use', 'id': 'call-2', 'name': 'mcp__reveal__query_claims', 'input': {'q': 'LEPR'}}),
       result('call-2', [{'type': 'text', 'text': '3 claims'}, {'type': 'image'}]),
       message('m3'), start(0, type='text', text=''), delta(0, 'text_delta', text='The answer.'),
       assistant('m3', {'type': 'text', 'text': 'The answer.'}),
       {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'The answer.', 'num_turns': 3, 'duration_ms': 4100,
        'total_cost_usd': 0.12}]


def shape(turns):
    return [(turn['role'], turn['kind'], turn['tool_name'], turn['is_error'], turn['partial']) for turn in turns]


class FoldTests(unittest.TestCase):
    def test_deltas_fold_into_whole_messages_and_tool_calls_pair_with_their_results(self):
        turns, cut, skipped = fold(raw(*RUN))
        self.assertEqual(shape(turns), [
            ('system', 'text', None, False, False),
            ('assistant', 'thinking', None, False, False),
            ('assistant', 'tool_use', 'Bash', False, False),
            ('tool', 'tool_result', 'Bash', True, False),
            ('assistant', 'tool_use', 'mcp__reveal__query_claims', False, False),
            ('tool', 'tool_result', 'mcp__reveal__query_claims', False, False),
            ('assistant', 'text', None, False, False),
            ('result', 'result', None, False, False)])
        self.assertEqual((cut, skipped), (False, 1))   # the rate-limit notice is not a turn
        self.assertEqual([turn['index'] for turn in turns], list(range(8)))
        self.assertEqual(turns[1]['text'], 'Plan: fetch it.')
        self.assertEqual((turns[2]['tool_input'], turns[2]['call_id'], turns[3]['call_id']), ('{"command":"curl -sSL x"}', 'call-1', 'call-1'))
        self.assertEqual(turns[3]['text'], 'curl: (6) Could not resolve host')
        self.assertEqual(turns[5]['text'], '3 claims\n[image]')
        self.assertEqual(turns[7]['text'], 'The answer.')
        self.assertEqual(turns[7]['detail'], {'subtype': 'success', 'stop_reason': None, 'num_turns': 3, 'duration_ms': 4100,
                                              'duration_api_ms': None, 'total_cost_usd': 0.12})
        self.assertEqual(turns[0]['detail']['tools'], ['Read', 'Bash'])
        self.assertIn('2.1.282', turns[0]['text'])
        self.assertEqual([turn['at'] for turn in turns], ['2026-11-19T10:00:00.000Z'] * 3 + ['2026-11-19T10:00:03.000Z'] * 5)

    def test_a_message_the_run_never_completed_is_kept_from_its_deltas_once(self):
        stopped = [message('m1'), start(0, type='text', text=''), delta(0, 'text_delta', text='Done with the first part.'),
                   assistant('m1', {'type': 'text', 'text': 'Done with the first part.'}),
                   start(1, type='tool_use', id='call-9', name='Bash', input={}), delta(1, 'input_json_delta', partial_json='{"comm'),
                   message('m2'), start(0, type='text', text=''), delta(0, 'text_delta', text='Half a sen')]
        turns, _, _ = fold(raw(*stopped))
        self.assertEqual(shape(turns), [('assistant', 'text', None, False, False), ('assistant', 'tool_use', 'Bash', False, True),
                                        ('assistant', 'text', None, False, True)])
        self.assertEqual((turns[1]['tool_input'], turns[1]['call_id'], turns[2]['text']), ('{"comm', 'call-9', 'Half a sen'))

    def test_a_cut_is_a_turn_where_the_lines_were_left_out(self):
        cut_line = {'type': TRANSCRIPT_CUT, 'omitted_bytes': 12345, 'omitted_lines': 40, 'original_bytes': 99999}
        turns, cut, _ = fold(raw(INIT, message('m1'), start(0, type='text', text=''), delta(0, 'text_delta', text='Before the cut'),
                                 cut_line, result('call-x', 'late result')))
        self.assertTrue(cut)
        self.assertEqual(shape(turns), [('system', 'text', None, False, False), ('assistant', 'text', None, False, True),
                                        ('system', 'text', None, False, False), ('tool', 'tool_result', None, False, False)])
        self.assertIn('12,345 bytes (40 lines)', turns[2]['text'])
        self.assertEqual(turns[2]['detail'], {'omitted_bytes': 12345, 'omitted_lines': 40, 'original_bytes': 99999})

    def test_text_and_tool_input_are_redacted_then_capped(self):
        with patch.dict(os.environ, {'PROVIDER_API_KEY': 'environment-secret-value'}):
            turns, _, skipped = fold(raw(
                assistant('m1', {'type': 'text', 'text': 'key environment-secret-value then ' + 'x' * MAX_TEXT},
                          {'type': 'tool_use', 'id': 'c', 'name': 'Bash', 'input': {'command': 'curl -H "Authorization: Bearer abc.def"',
                                                                                   'token': 'field-secret', 'pad': 'y' * MAX_TEXT}}))
                + b'not json\n')
        text, call = turns
        self.assertTrue(text['truncated']); self.assertEqual(len(text['text']), MAX_TEXT)
        self.assertTrue(text['text'].startswith('key [redacted] then '))
        self.assertTrue(call['tool_input_truncated']); self.assertEqual(len(call['tool_input']), MAX_TEXT)
        self.assertNotIn('abc.def', call['tool_input']); self.assertNotIn('field-secret', call['tool_input'])
        self.assertEqual(skipped, 1)


class TranscriptReadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = Repository(str(self.root / 'app.sqlite')); self.repo.migrate()
        for p in (patch.object(api, 'repo', self.repo), patch.dict(os.environ, {
                'REVEAL_ARTIFACTS_DIR': str(self.root / 'artifacts'), 'REVEAL_ARTIFACT_STORE': 'filesystem',
                'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_SERVICE_TOKEN': 't' * 40, 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
                'REVEAL_GATEWAY_AUDIENCE': 'reveal-api', 'DISABLE_ADMIN_LOGIN': 'true'})):
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(api.app)

    def headers(self, purpose='admin_content'):
        claims = {'sub': 'admin-console', 'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()), 'exp': int(time.time()) + 120,
                  'jti': uid(), 'purpose': purpose, 'admin_email': ADMIN}
        return {'Authorization': 'Bearer ' + 't' * 40, 'X-Reveal-Admin-Assertion': jwt.encode(claims, 's' * 40, algorithm='HS256')}

    def job(self, attempts=(1,)):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner-1', 'agent', inputs=frozen())
            for attempt in attempts:
                tx.put('attempt', f"{job['id']}:{attempt}", 'owner-1', {'attempt': attempt, 'started_at': now()})
        return job['id']

    def save(self, identity, relative, data, attempt=1):
        path = self.root / 'artifacts' / identity / relative.format(attempt)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)

    def reads(self):
        with self.repo.read_transaction() as tx:
            return [row['data'] for row in tx.list('admin_content_read')]

    def get(self, identity, **query):
        return self.client.get(f'/internal/v1/admin/jobs/{identity}/transcript', params=query, headers=self.headers())

    def test_only_an_admin_content_assertion_reads_it_and_every_page_is_recorded(self):
        identity = self.job(); self.save(identity, STREAM, gzip.compress(raw(*RUN)))
        url = f'/internal/v1/admin/jobs/{identity}/transcript'
        self.assertEqual(self.client.get(url).status_code, 403)
        for purpose in ('admin_telemetry', 'admin_settings', 'verified_identity'):
            self.assertEqual(self.client.get(url, headers=self.headers(purpose)).status_code, 401, purpose)
        self.assertEqual(self.reads(), [])
        first = self.get(identity, limit=3)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers['cache-control'], 'private, no-store')
        second = self.get(identity, after=first.json()['next_after'], limit=3)
        reads = self.reads()
        self.assertEqual(sorted((r['admin'], r['job_id'], r['route'], r['attempt'], r['after'] or 0) for r in reads),
                         [(ADMIN, identity, 'jobs/transcript', 1, 0), (ADMIN, identity, 'jobs/transcript', 1, 2)])
        self.assertEqual(sum(r['after'] is None for r in reads), 1)
        self.assertNotIn('curl', json.dumps(reads))
        self.assertEqual(second.json()['read']['by'], ADMIN)

    def test_a_read_that_cannot_be_recorded_returns_nothing(self):
        identity = self.job(); self.save(identity, STREAM, gzip.compress(raw(*RUN)))
        with patch.object(Repository, 'append', side_effect=RuntimeError('database down')):
            response = self.get(identity)
        self.assertEqual((response.status_code, response.json()['code']), (503, 'ACCESS_LOG_UNAVAILABLE'))
        self.assertNotIn('curl', response.text)

    def test_pages_follow_the_turn_index(self):
        identity = self.job(); self.save(identity, STREAM, gzip.compress(raw(*RUN)))
        seen, after = [], None
        while True:
            page = self.get(identity, limit=3, **({'after': after} if after is not None else {})).json()
            self.assertEqual((page['total'], page['attempt'], page['latest_attempt'], page['missing'], page['cut'], page['complete']),
                             (8, 1, 1, None, False, True))
            seen += [turn['index'] for turn in page['turns']]
            after = page['next_after']
            if not page['has_more']: break
        self.assertEqual(seen, list(range(8)))
        end = self.get(identity, after=7).json()
        self.assertEqual((end['turns'], end['next_after'], end['has_more']), ([], 7, False))
        for query in ({'limit': 201}, {'limit': 0}, {'after': -1}, {'attempt': 0}):
            self.assertEqual(self.get(identity, **query).status_code, 422, query)
        with self.assertRaises(Exception): job_transcript(self.repo, identity, ADMIN, limit=500)

    def test_a_job_without_a_captured_transcript_says_so(self):
        identity = self.job()
        response = self.get(identity)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body['turns'], body['total'], body['missing'], body['stderr'], body['next_after']), ([], 0, 'not_captured', None, None))
        self.assertEqual(len(self.reads()), 1)
        queued = self.job(attempts=())
        self.assertEqual((self.get(queued).json()['attempt'], self.get(queued).json()['missing']), (None, 'not_captured'))

    def test_other_jobs_and_unknown_ids_have_no_transcript(self):
        with self.repo.transaction() as tx: analysis = jobs.enqueue(tx, 'owner-1', 'analysis', inputs={'note': 'x'})['id']
        for identity, code in ((analysis, 'NOT_AN_AGENT_JOB'), (uid(), 'JOB_NOT_FOUND'), ('not-a-uuid', 'JOB_NOT_FOUND')):
            response = self.get(identity)
            self.assertEqual((response.status_code, response.json()['code']), (404, code))
        self.assertEqual(self.reads(), [])

    def test_attempts_stderr_and_damaged_files(self):
        identity = self.job(attempts=(1, 2))
        self.save(identity, STREAM, gzip.compress(raw(INIT)), attempt=1)
        stream = gzip.compress(raw(*RUN))
        self.save(identity, STREAM, stream[:len(stream) // 2], attempt=2)   # the runner stopped mid-write
        self.save(identity, STDERR, gzip.compress(b'npm warn\n' + b'e' * 30_000 + b'\nlast line\n'), attempt=2)
        newest = self.get(identity).json()
        self.assertEqual((newest['attempt'], newest['complete']), (2, False))
        self.assertGreater(newest['total'], 0)
        self.assertTrue(newest['stderr']['truncated'] and newest['stderr']['text'].endswith('last line\n'))
        self.assertEqual(len(newest['stderr']['text']), 20_000)
        first = self.get(identity, attempt=1).json()
        self.assertEqual((first['attempt'], first['total'], first['complete'], first['stderr']), (1, 1, True, None))
        self.save(identity, STREAM, b'not gzip', attempt=1)
        self.assertEqual(self.get(identity, attempt=1).json()['missing'], 'unreadable')
        self.assertEqual(self.get(identity, attempt=3).json()['missing'], 'not_captured')

    def test_on_s3_the_transcript_is_read_from_the_workspace_checkpoint(self):
        identity = self.job()
        stream = gzip.compress(raw(*RUN))
        blobs = {'workspace-ref': json.dumps({'format': 'reveal.workspace/1', 'files': [
            {'path': STREAM.format(1), 'storage': {'key': 'stream-ref', 'size_bytes': len(stream)}}]}).encode(), 'stream-ref': stream}
        storage = SimpleNamespace(get=lambda ref: blobs[ref if isinstance(ref, str) else ref['key']])
        with self.repo.transaction() as tx:
            queue = tx.get('queue', identity)['data']; queue['workspace'] = 'workspace-ref'; tx.put('queue', identity, 'owner-1', queue)
        with patch.dict(os.environ, {'REVEAL_ARTIFACT_STORE': 's3'}), patch('reveal_backend.artifact_store.store', return_value=storage):
            body = job_transcript(self.repo, identity, ADMIN)
            self.assertEqual((body['total'], body['missing']), (8, None))
            blobs['workspace-ref'] = b'not json'
            self.assertEqual(job_transcript(self.repo, identity, ADMIN)['missing'], 'unavailable')

    # The run view's configuration --------------------------------------------------------------------------------

    def runtime(self, identity, attempt=1, **extra):
        value = {'job_id': identity, 'attempt': attempt,
                 'configuration': {'format': 'reveal.run-configuration/1', 'claude_code': {'pinned_version': '2.1.282', 'observed_version': '2.1.282 (Claude Code)'},
                                   'cli': {'argv': ['claude', '-p'], 'wrapper': ['setpriv'], 'prompt': 'stdin',
                                           'environment': {'ANTHROPIC_API_KEY': 'reveal-loopback-proxy', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '32000'}},
                                   'tools': {'builtin': ['Read'], 'mcp': [], 'mcp_servers': {}}, 'network': {'mode': 'allow-all'},
                                   'toolchain': {'stamp': 'a' * 64}, 'account_kit': None,
                                   'limits': {'execution': {'max_output_tokens': 32000}, 'agent': {'max_output_tokens': 32000}}, 'source_tools': None},
                 'observed_claude_runtime': {'claude_code_version': '2.1.282', 'model': 'm', 'permissionMode': 'bypassPermissions', 'tools': ['Read']},
                 'transcript': {'ledger/claude-stream.jsonl.gz': {'stored_bytes': 10, 'cut': False}}, **extra}
        self.save(identity, 'attempt-{}/output/runtime.json', json.dumps(value).encode(), attempt=attempt)
        return value

    def test_the_run_view_returns_the_newest_attempts_configuration(self):
        identity = self.job()
        missing = job_run(self.repo, identity, ADMIN)['configuration']
        self.assertEqual((missing['attempt'], missing['missing'], missing['cli'], missing['observed']), (1, 'not_captured', None, None))
        value = self.runtime(identity)
        configuration = self.client.get(f'/internal/v1/admin/jobs/{identity}/run', headers=self.headers()).json()['configuration']
        self.assertEqual(configuration['missing'], None)
        for key in ('format', 'claude_code', 'cli', 'tools', 'network', 'toolchain', 'account_kit', 'limits', 'source_tools'):
            self.assertEqual(configuration[key], value['configuration'][key], key)
        self.assertEqual(configuration['observed'], {'claude_code_version': '2.1.282', 'model': 'm', 'permission_mode': 'bypassPermissions',
                                                     'tools': ['Read'], 'mcp_servers': None})
        self.assertEqual(configuration['transcript'], value['transcript'])
        older = self.runtime(identity); older.pop('configuration')
        self.save(identity, 'attempt-{}/output/runtime.json', json.dumps(older).encode())
        self.assertEqual(job_run(self.repo, identity, ADMIN)['configuration']['missing'], 'not_recorded')
        self.runtime(identity, job_id=uid())
        self.assertEqual(job_run(self.repo, identity, ADMIN)['configuration']['missing'], 'unreadable')


if __name__ == '__main__':
    unittest.main()
