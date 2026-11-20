"""The admin content read of one agent job: its own authority, a durable access record, the full request, the limits
table with why the run stopped, and the full response."""
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
from reveal_backend import app as api, box_agent, box_remote, jobs, job_metrics
from reveal_backend.admin_run import MAX_RESPONSE_BYTES, job_run
from reveal_backend.job_failures import authoring_failure
from reveal_backend.repository import Repository, now, uid
from reveal_backend.telemetry import snapshot

ADMIN = 'admin@example.org'
LIMITS = {'timeout_seconds': 600, 'max_turns': 12, 'max_budget_usd': 1.0, 'tool_calls': 30, 'max_output_tokens': 32000}


def frozen(**body):
    return box_agent.freeze_input({'kind': 'agent', 'prompt': 'PROMPT-TEXT: list the LEPR papers.', 'system_prompt': 'SYSTEM-TEXT',
                                   'limits': dict(LIMITS), **body})


def runner_end(how, limits=LIMITS, model='claude-opus-5-5'):
    """(job status, job failure, job_metrics agent, Claude result) as the runner and the backend produce them for one way
    a run ends: box_remote's completion and reason, then job_failures and job_metrics."""
    request = {'job_id': 'x', 'attempt': 1, 'kind': 'agent', 'max_turns': limits['max_turns'], 'max_budget_usd': limits['max_budget_usd'],
               'timeout_seconds': limits['timeout_seconds']}
    started, status, reason = time.monotonic() - 42, 'failed', None
    if how == 'turns':
        result = {'is_error': True, 'subtype': 'error_max_turns', 'num_turns': limits['max_turns'], 'total_cost_usd': .31}
    elif how == 'budget':
        result = {'is_error': True, 'subtype': 'error_max_budget_usd', 'num_turns': 7, 'total_cost_usd': 1.0412}
    elif how == 'output':
        result = {'is_error': True, 'result': f"API Error: Claude's response exceeded the {limits['max_output_tokens']} output token maximum.",
                  'stop_reason': 'max_tokens', 'num_turns': 4, 'total_cost_usd': .2}
    elif how == 'timeout':
        result, started = None, time.monotonic() - limits['timeout_seconds']
        reason = box_remote.deadline_reason(request, {'kind': 'tool_call', 'tool_name': 'Bash'})
    else:
        result, status = {'is_error': False, 'subtype': 'success', 'num_turns': 5, 'total_cost_usd': .12, 'result': 'RESPONSE-TEXT'}, 'succeeded'
    if result is not None and status == 'failed':
        reason = box_remote.provider_failure_reason(request, result, limits['max_output_tokens'])
    parser = SimpleNamespace(result=result, timing=None)
    completion = box_remote.runtime_completion(request, started, status, reason, parser=parser, max_output_tokens=limits['max_output_tokens'])
    runtime = {'model': model, 'completion': completion, 'execution_limits': {k: request[k] for k in ('timeout_seconds', 'max_budget_usd', 'max_turns')}}
    failure = None
    if status == 'failed':
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runtime.json'; path.write_text(json.dumps(runtime))
            failure = authoring_failure(SimpleNamespace(runtime_manifest_path=path, reason=reason),
                                        SimpleNamespace(kind='agent', max_budget_usd=limits['max_budget_usd']), 128000)
    return status, failure, job_metrics.agent_metrics(runtime), result


class AdminRunTests(unittest.TestCase):
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

    def headers(self, purpose='admin_content', **extra):
        claims = {'sub': 'admin-console', 'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()), 'exp': int(time.time()) + 120,
                  'jti': uid(), 'purpose': purpose, **({'admin_email': ADMIN} if purpose == 'admin_content' else {}), **extra}
        return {'Authorization': 'Bearer ' + 't' * 40, 'X-Reveal-Admin-Assertion': jwt.encode(claims, 's' * 40, algorithm='HS256')}

    def agent_job(self, inputs=None, how=None, requested=None, ceilings=None, response=None, attribution=False):
        """An agent job, ended `how` (None: still queued), with its response record saved when `response` is given."""
        inputs = inputs or frozen()
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner-1', 'agent', inputs=inputs)
            queue = tx.get('queue', job['id'])['data']
            queue['token'] = 'LEASE-TOKEN'
            if requested is not None: queue['requested_limits'] = requested
            if ceilings is not None: queue['limit_ceilings'] = ceilings
            if attribution: queue['attribution'] = {'user_id': 'owner-1', 'display_name': 'REQUESTER-NAME', 'orcid': '0000-0002-1825-0097'}
            tx.put('queue', job['id'], 'owner-1', queue)
            tx.put('attempt', job['id'] + ':1', 'owner-1', {'attempt': 1, 'started_at': now()})
            if how:
                status, failure, metrics, result = runner_end(how, inputs['limits'])
                record = box_agent.response_record(job['id'], 1, status, {**(result or {}), **({'result': response} if response else {})})
                files = {'output/table.csv': {'size_bytes': 120, 'sha256': 'c' * 64}}
                job_result = box_agent.job_result(record, files, job_id=job['id'], attempt=1) if status == 'succeeded' else None
                job.update(status=status, stage='complete', failure=failure, result=job_result, completed_at=now())
                tx.put('job', job['id'], 'owner-1', job)
                tx.put('job_metrics', job['id'], 'owner-1', {'format': 'reveal.job-metrics/1', 'kind': 'agent', 'job_id': job['id'], 'agent': metrics})
                if response is not None:
                    path = self.root / 'artifacts' / job['id'] / 'attempt-1/output/ledger/agent-response.json'
                    path.parent.mkdir(parents=True); path.write_text(json.dumps(record))
        return job['id']

    def reads(self):
        with self.repo.read_transaction() as tx:
            return [row['data'] for row in tx.list('admin_content_read')]

    def test_only_an_admin_content_assertion_naming_an_admin_reads_it(self):
        url = '/internal/v1/admin/jobs/' + self.agent_job() + '/run'
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url, headers={'Authorization': 'Bearer ' + 't' * 40}).status_code, 401)
        # The telemetry and settings purposes, and a non-admin session, do not open the content.
        for purpose in ('admin_telemetry', 'admin_settings', 'verified_identity'):
            self.assertEqual(self.client.get(url, headers=self.headers(purpose, admin_email=ADMIN)).status_code, 401, purpose)
        no_admin = self.headers('admin_content'); no_admin['X-Reveal-Admin-Assertion'] = jwt.encode(
            {'sub': 'admin-console', 'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()), 'exp': int(time.time()) + 120,
             'jti': uid(), 'purpose': 'admin_content'}, 's' * 40, algorithm='HS256')
        self.assertEqual(self.client.get(url, headers=no_admin).status_code, 401)
        self.assertEqual(self.reads(), [])
        response = self.client.get(url, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(response.json()['input']['prompt'], 'PROMPT-TEXT: list the LEPR papers.')

    def test_every_read_is_recorded_with_the_admin_job_and_time_and_never_the_content(self):
        identity = self.agent_job(how='normal', response='RESPONSE-TEXT', requested=['max_turns'])
        for _ in range(2): self.assertEqual(self.client.get(f'/internal/v1/admin/jobs/{identity}/run', headers=self.headers()).status_code, 200)
        reads = self.reads()
        self.assertEqual(len(reads), 2)
        self.assertEqual({(r['format'], r['admin'], r['job_id'], r['route']) for r in reads},
                         {('reveal.admin-content-read/1', ADMIN, identity, 'jobs/run')})
        self.assertTrue(all(r['read_at'] for r in reads))
        for text in ('PROMPT-TEXT', 'SYSTEM-TEXT', 'RESPONSE-TEXT', 'owner-1'): self.assertNotIn(text, json.dumps(reads))

    def test_a_read_that_cannot_be_recorded_returns_nothing(self):
        identity = self.agent_job(how='normal', response='RESPONSE-TEXT')
        with patch.object(Repository, 'append', side_effect=RuntimeError('database down')):
            response = self.client.get(f'/internal/v1/admin/jobs/{identity}/run', headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'ACCESS_LOG_UNAVAILABLE')
        self.assertNotIn('PROMPT-TEXT', response.text); self.assertNotIn('RESPONSE-TEXT', response.text)

    def test_other_jobs_and_unknown_ids_have_no_request_to_show(self):
        with self.repo.transaction() as tx: analysis = jobs.enqueue(tx, 'owner-1', 'analysis', inputs={'note': 'x'})['id']
        for identity, code in ((analysis, 'NOT_AN_AGENT_JOB'), (uid(), 'JOB_NOT_FOUND'), ('not-a-uuid', 'JOB_NOT_FOUND')):
            response = self.client.get(f'/internal/v1/admin/jobs/{identity}/run', headers=self.headers())
            self.assertEqual((response.status_code, response.json()['code']), (404, code))
        self.assertEqual(self.reads(), [])

    def test_the_frozen_input_is_returned_in_full_without_the_lease_attribution_or_package(self):
        from reveal_backend import agent_account
        answer = self.agent_job(frozen(tools=['Read', 'Bash'], network='restricted', allowed_domains=['eutils.ncbi.nlm.nih.gov'], effort='high'))
        view = job_run(self.repo, answer, ADMIN)
        self.assertEqual(view['input'], {'format': 'reveal.agent-input/5', 'prompt': 'PROMPT-TEXT: list the LEPR papers.', 'system_prompt': 'SYSTEM-TEXT',
                                         'tools': ['Read', 'Bash'], 'graphs': [], 'network': 'restricted', 'allowed_domains': ['eutils.ncbi.nlm.nih.gov'],
                                         'effort': 'high', 'model': box_agent.DEFAULT_MODEL, 'deliverable': 'answer', 'claims_repository': False,
                                         'output_schema': None, 'limits': LIMITS, 'account': None})
        self.assertEqual(view['job']['owner'], 'owner-1')
        inputs = box_agent.freeze_input({'kind': 'agent', 'prompt': 'PROMPT-TEXT', 'deliverable': 'scientific_account', 'question': 'QUESTION-TEXT?',
                                         'claims': [{'statement': 'CLAIM-TEXT', 'subject_entity': 'HGNC.SYMBOL:LEPR', 'relation': 'biolink:associated_with',
                                                     'object_entity': 'MONDO:0005148'}], 'attribute_requester': True},
                                        account_builder=agent_account.build)
        account = job_run(self.repo, self.agent_job(inputs, attribution=True), ADMIN)
        claim = account['input']['account']['claims'][0]
        self.assertEqual((account['input']['account']['question'], account['input']['account']['attribute_requester'], claim['statement'],
                          claim['subject_entity']), ('QUESTION-TEXT?', True, 'CLAIM-TEXT', 'HGNC.SYMBOL:LEPR'))
        self.assertTrue(claim['proposition_id'].startswith('dapper:Proposition.'))
        encoded = json.dumps(account)
        for hidden in ('LEASE-TOKEN', 'REQUESTER-NAME', '0000-0002-1825-0097', 'dapper_context', 'package'):
            self.assertNotIn(hidden, encoded)

    def test_structured_output_is_asked_for_and_returned(self):
        schema = {'type': 'object', 'properties': {'genes': {'type': 'array', 'items': {'type': 'string'}}}}
        from reveal_backend import agent_output
        inputs = box_agent.freeze_input({'kind': 'agent', 'prompt': 'P', 'output_schema': schema}, schema_checker=agent_output.check_schema)
        identity = self.agent_job(inputs)
        with self.repo.transaction() as tx:
            job = tx.get('job', identity)['data']
            record = box_agent.response_record(identity, 1, 'succeeded', {'result': 'done', 'structured_output': {'genes': ['LEPR']}, 'subtype': 'success'})
            job.update(status='succeeded', result=box_agent.job_result(record, {}, job_id=identity, attempt=1), completed_at=now())
            tx.put('job', identity, 'owner-1', job)
        view = job_run(self.repo, identity, ADMIN)
        self.assertEqual(view['input']['output_schema'], schema)
        # No captured record: the job result's copy, which carries the structured output too.
        self.assertEqual((view['response']['source'], view['response']['structured_output']), ('job_result', {'genes': ['LEPR']}))

    def limits(self, view):
        return {row['key']: row for row in view['limits']}

    def test_a_run_stopped_at_its_own_max_turns(self):
        view = job_run(self.repo, self.agent_job(how='turns', requested=['max_turns'], ceilings={'timeout_seconds': 3600, 'max_turns': 100,
                       'max_budget_usd': 5.0, 'tool_calls': 300, 'max_output_tokens': 128000}), ADMIN)
        self.assertEqual(view['stop'], {'limit': 'max_turns', 'by': 'request', 'text': 'Stopped at its own max_turns (12), which the request set.'})
        turns = self.limits(view)['max_turns']
        self.assertEqual({k: turns[k] for k in ('requested', 'requested_basis', 'frozen', 'ceiling', 'ceiling_basis', 'applied', 'used', 'hit')},
                         {'requested': True, 'requested_basis': 'recorded', 'frozen': 12, 'ceiling': 100, 'ceiling_basis': 'queue_time',
                          'applied': 12, 'used': 12, 'hit': True})
        self.assertEqual([row['key'] for row in view['limits'] if row['hit']], ['max_turns'])
        self.assertIs(self.limits(view)['timeout_seconds']['requested'], False)
        self.assertEqual(view['job']['failure']['code'], 'AGENT_EXECUTION_FAILED')

    def test_a_run_that_hit_its_budget(self):
        view = job_run(self.repo, self.agent_job(how='budget', requested=[]), ADMIN)
        self.assertEqual(view['stop'], {'limit': 'max_budget_usd', 'by': 'default', 'text': 'Hit its budget ($1.00), the default it was given.'})
        budget = self.limits(view)['max_budget_usd']
        self.assertEqual((budget['used'], budget['used_estimated'], budget['applied'], budget['hit']), (1.0412, False, 1, True))
        # No ceilings recorded on this job: today's are shown and marked so.
        self.assertEqual((budget['ceiling'], budget['ceiling_basis']), (5.0, 'current'))

    def test_a_budget_lowered_to_the_admin_ceiling_at_dispatch_names_the_ceiling(self):
        identity = self.agent_job(frozen(limits={**LIMITS, 'max_budget_usd': 4.0}), how='budget', requested=['max_budget_usd'])
        with self.repo.transaction() as tx:
            row = tx.get('job_metrics', identity)['data']; row['agent']['budget_usd'] = 1.0
            tx.put('job_metrics', identity, 'owner-1', row)
            job = tx.get('job', identity)['data']; job['failure']['budget']['limit_usd'] = 1.0
            tx.put('job', identity, 'owner-1', job)
        stop = job_run(self.repo, identity, ADMIN)['stop']
        self.assertEqual(stop, {'limit': 'max_budget_usd', 'by': 'admin_ceiling', 'text': 'Hit the admin budget ceiling ($1.00); the job was frozen with $4.00.'})

    def test_a_run_that_timed_out(self):
        view = job_run(self.repo, self.agent_job(how='timeout', requested=['timeout_seconds', 'max_turns']), ADMIN)
        self.assertEqual(view['stop'], {'limit': 'timeout_seconds', 'by': 'request', 'text': 'Timed out at 600 s, which the request set.'})
        row = self.limits(view)['timeout_seconds']
        self.assertTrue(row['hit']); self.assertGreaterEqual(row['used'], 600)
        self.assertIsNone(view['response']['source'])   # a run stopped at its deadline leaves no response record

    def test_a_response_that_exceeded_max_output_tokens(self):
        view = job_run(self.repo, self.agent_job(how='output', requested=['max_output_tokens']), ADMIN)
        self.assertEqual(view['stop'], {'limit': 'max_output_tokens', 'by': 'request',
                                        'text': 'A response exceeded max_output_tokens (32,000), which the request set.'})
        row = self.limits(view)['max_output_tokens']
        self.assertEqual((row['hit'], row['reached'], row['frozen']), (True, True, 32000))
        self.assertEqual(view['job']['failure']['code'], 'AGENT_OUTPUT_TOKENS_EXCEEDED')

    def test_a_run_that_finished_normally_hits_no_limit(self):
        view = job_run(self.repo, self.agent_job(how='normal', response='RESPONSE-TEXT', requested=[]), ADMIN)
        self.assertEqual(view['stop'], {'limit': None, 'by': None, 'text': 'Finished normally.'})
        self.assertFalse(any(row['hit'] for row in view['limits']))
        self.assertIs(self.limits(view)['max_output_tokens']['reached'], False)
        self.assertEqual(self.limits(view)['max_turns']['used'], 5)
        # REVEAL tool calls are not counted on their own: no used value, never a hit.
        self.assertIsNone(self.limits(view)['tool_calls']['used'])

    def test_a_failure_that_is_no_limit_names_its_code_and_claims_no_cause(self):
        identity = self.agent_job()
        with self.repo.transaction() as tx:
            job = tx.get('job', identity)['data']
            job.update(status='failed', failure={'code': 'AGENT_EXECUTION_FAILED', 'message': 'BoxTransportError: trusted agent execution failed'})
            tx.put('job', identity, 'owner-1', job)
        stop = job_run(self.repo, identity, ADMIN)['stop']
        self.assertEqual(stop, {'limit': None, 'by': None, 'text': 'Ended failed (AGENT_EXECUTION_FAILED: BoxTransportError: trusted agent execution '
                                                                   'failed). No limit was identified as the cause.'})

    def test_a_queued_job_has_not_stopped(self):
        view = job_run(self.repo, self.agent_job(), ADMIN)
        self.assertEqual(view['stop'], {'limit': None, 'by': None, 'text': 'Not finished: queued.'})
        self.assertIsNone(self.limits(view)['max_output_tokens']['reached'])

    def test_a_job_queued_before_requested_limits_were_recorded_says_so(self):
        view = job_run(self.repo, self.agent_job(how='turns'), ADMIN)
        rows = self.limits(view)
        self.assertEqual({key: (rows[key]['requested'], rows[key]['requested_basis']) for key in rows},
                         dict.fromkeys(rows, (None, 'not_recorded')))
        # Compared with today's defaults only: 12 turns differs from 50; the 30 REVEAL tool calls do not differ.
        self.assertEqual((rows['max_turns']['differs_from_current_default'], rows['tool_calls']['differs_from_current_default']), (True, False))
        self.assertEqual(view['stop']['by'], 'unknown')
        self.assertEqual(view['stop']['text'], 'Stopped at its own max_turns (12). Whether the request set it is not recorded for this job.')

    def test_the_full_response_comes_from_the_captured_record(self):
        long = 'RESPONSE-TEXT ' + 'x' * 200_000   # above the 50,000-character job result and the 128 KiB diagnostic preview
        view = job_run(self.repo, self.agent_job(how='normal', response=long), ADMIN)
        response = view['response']
        self.assertEqual((response['source'], response['truncated'], response['characters'], response['text']), ('captured', False, len(long), long))
        self.assertEqual(response['files'], [{'path': 'table.csv', 'size_bytes': 120, 'sha256': 'c' * 64}])
        self.assertEqual((response['status'], response['subtype']), ('succeeded', 'success'))

    def test_without_the_captured_record_the_job_results_copy_is_marked_truncated(self):
        long = 'RESPONSE-TEXT ' + 'x' * 60_000
        identity = self.agent_job(how='normal', response=long)
        (self.root / 'artifacts' / identity / 'attempt-1/output/ledger/agent-response.json').unlink()
        response = job_run(self.repo, identity, ADMIN)['response']
        self.assertEqual((response['source'], response['truncated'], response['characters'], response['captured_unavailable']),
                         ('job_result', True, box_agent.MAX_RESULT_RESPONSE_CHARS, 'not_captured'))

    def test_a_record_from_another_execution_or_too_large_is_not_trusted(self):
        identity = self.agent_job(how='normal', response='RESPONSE-TEXT')
        path = self.root / 'artifacts' / identity / 'attempt-1/output/ledger/agent-response.json'
        record = json.loads(path.read_text()); record['job_id'] = uid(); path.write_text(json.dumps(record))
        response = job_run(self.repo, identity, ADMIN)['response']
        self.assertEqual((response['source'], response['captured_unavailable']), ('job_result', 'unreadable'))
        path.write_text(' ' * (MAX_RESPONSE_BYTES + 1))
        self.assertEqual(job_run(self.repo, identity, ADMIN)['response']['captured_unavailable'], 'too_large')

    def test_on_s3_the_record_is_read_from_the_workspace_checkpoint(self):
        identity = self.agent_job(how='normal', response='RESPONSE-TEXT ' + 'y' * 70_000)
        local = self.root / 'artifacts' / identity / 'attempt-1/output/ledger/agent-response.json'
        raw = local.read_bytes(); local.unlink()
        blobs = {'workspace-ref': json.dumps({'format': 'reveal.workspace/1', 'files': [
            {'path': 'attempt-1/output/ledger/agent-response.json', 'storage': {'key': 'response-ref', 'size_bytes': len(raw)}}]}).encode(),
            'response-ref': raw}
        storage = SimpleNamespace(get=lambda ref: blobs[ref if isinstance(ref, str) else ref['key']])
        with self.repo.transaction() as tx:
            queue = tx.get('queue', identity)['data']; queue['workspace'] = 'workspace-ref'; tx.put('queue', identity, 'owner-1', queue)
        with patch.dict(os.environ, {'REVEAL_ARTIFACT_STORE': 's3'}), patch('reveal_backend.artifact_store.store', return_value=storage):
            response = job_run(self.repo, identity, ADMIN)['response']
            self.assertEqual((response['source'], response['characters']), ('captured', 70_014))
            blobs['workspace-ref'] = b'not json'
            self.assertEqual(job_run(self.repo, identity, ADMIN)['response']['captured_unavailable'], 'unavailable')

    def test_only_the_latest_attempts_response_is_shown(self):
        identity = self.agent_job(how='timeout', response='RESPONSE-TEXT from attempt one')
        # The record above belongs to attempt 1; a retry ran attempt 2 and stopped at its deadline without one.
        with self.repo.transaction() as tx: tx.put('attempt', identity + ':2', 'owner-1', {'attempt': 2, 'started_at': now()})
        response = job_run(self.repo, identity, ADMIN)['response']
        self.assertEqual((response['source'], response['text'], response['captured_unavailable']), (None, None, 'not_captured'))
        with self.repo.transaction() as tx: tx.remove('attempt', identity + ':2')
        response = job_run(self.repo, identity, ADMIN)['response']
        self.assertEqual((response['source'], response['attempt']), ('captured', 1))

    def test_claims_frozen_as_plain_statements_are_shown(self):
        inputs = frozen()
        inputs = {**inputs, 'deliverable': 'scientific_account', 'account': {'question': 'QUESTION-TEXT?', 'claims': ['CLAIM-TEXT one'], 'package': {}}}
        claims = job_run(self.repo, self.agent_job(inputs), ADMIN)['input']['account']['claims']
        self.assertEqual(claims, [{'statement': 'CLAIM-TEXT one'}])

    def test_the_overview_still_selects_no_content_while_this_read_does(self):
        identity = self.agent_job(how='normal', response='RESPONSE-TEXT', attribution=True)
        from reveal_backend.admin_jobs import job_detail
        overview = json.dumps(snapshot(self.repo)) + json.dumps(job_detail(self.repo, identity)['agent'])
        for text in ('PROMPT-TEXT', 'SYSTEM-TEXT', 'RESPONSE-TEXT'): self.assertNotIn(text, overview)
        content = json.dumps(job_run(self.repo, identity, ADMIN))
        for text in ('PROMPT-TEXT', 'SYSTEM-TEXT', 'RESPONSE-TEXT'): self.assertIn(text, content)
        for text in ('LEASE-TOKEN', 'REQUESTER-NAME'): self.assertNotIn(text, content)


if __name__ == '__main__':
    unittest.main()
