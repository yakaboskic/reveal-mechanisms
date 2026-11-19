"""Admin credential isolation, bounded projections, and durable execution timing."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import jwt
from fastapi.testclient import TestClient
from reveal_backend import app as api, jobs
from reveal_backend.repository import Repository, now, uid
from reveal_backend.telemetry import snapshot, seconds


class AdminTelemetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.repo = Repository(str(Path(tmp.name)/'app.sqlite')); self.repo.migrate()
        for p in [patch.object(api, 'repo', self.repo), patch.dict(os.environ, {
            'REVEAL_GATEWAY_SECRET': 's'*40, 'REVEAL_GATEWAY_SERVICE_TOKEN': 't'*40,
            'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api',
            'DISABLE_ADMIN_LOGIN': 'true'})]:
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(api.app)

    def proof(self, purpose='admin_telemetry', **extra):
        return jwt.encode({'sub':'admin-console', 'iss':'reveal-nextjs', 'aud':'reveal-api',
            'iat':int(time.time()), 'exp':int(time.time())+120, 'jti':uid(), 'purpose':purpose, **extra}, 's'*40, algorithm='HS256')

    def test_backend_requires_service_and_scoped_proof_even_with_bypass(self):
        url = '/internal/v1/admin/telemetry'
        self.assertEqual(self.client.get(url).status_code, 403)
        service = {'Authorization':'Bearer '+'t'*40}
        self.assertEqual(self.client.get(url, headers=service).status_code, 401)
        self.assertEqual(self.client.get(url, headers={**service, 'X-Reveal-Admin-Assertion':self.proof('verified_identity')}).status_code, 401)
        self.assertEqual(self.client.get(url, headers={**service, 'X-Reveal-Admin-Assertion':self.proof(exp=int(time.time())-1)}).status_code, 401)
        self.assertEqual(self.client.get(url, headers={'Authorization':'Bearer '+self.proof(), 'X-Reveal-Admin-Assertion':self.proof()}).status_code, 403)
        result = self.client.get(url, headers={**service, 'X-Reveal-Admin-Assertion':self.proof()})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers['cache-control'], 'private, no-store')
        self.assertEqual(result.json()['database'], 'sqlite-test')

    def test_projection_excludes_payloads_and_includes_cross_workspace_counts(self):
        with self.repo.transaction() as tx:
            for i in range(105): tx.put('draft', str(i), 'one' if i % 2 else 'two', {'secret':'private research'})
            job = jobs.enqueue(tx, 'owner', 'analysis')
        claimed, queue = jobs.claim(self.repo, 'worker-1')
        with self.repo.transaction() as tx:
            queue['remote_handle'] = {'box_id':'box-1','phase':'deleted','created_at':100,
                'timings':{'prepared_at':110,'running_at':111,'terminal_at':141,'captured_at':145,'deleted_at':146}}
            tx.put('queue', job['id'], 'owner', queue)
        jobs.finish(self.repo, job['id'], queue['token'], 'failed', failure={'code':'WORKER_FAILED','message':'private error'})
        data = snapshot(self.repo)
        self.assertEqual(next(x['count'] for x in data['counts'] if x['kind']=='draft'), 105)
        self.assertEqual(len(data['recent']), 100)
        self.assertEqual(data['statuses'], [{'status':'failed', 'count':1}])
        timing = data['jobs'][0]
        self.assertEqual([timing[k] for k in ('setup_seconds','agent_seconds','capture_seconds','cleanup_seconds','box_seconds')], [10,30,4,1,46])
        self.assertFalse(timing['lease_expired'])
        encoded = json.dumps(data)
        for secret in ('private research', 'private error', queue['token'], 'dispatch_input'):
            self.assertNotIn(secret, encoded)

    def test_recovery_preserves_attempt_start(self):
        with self.repo.transaction() as tx: job = jobs.enqueue(tx, 'owner', 'analysis')
        _, queue = jobs.claim(self.repo, 'worker-1')
        identity = f"{job['id']}:1"
        with self.repo.transaction() as tx:
            attempt = tx.get('attempt', identity)['data']; attempt['started_at'] = '2020-01-01T00:00:00Z'
            tx.put('attempt', identity, 'owner', attempt)
            queue.update(lease_until='2020-01-01T00:00:00Z', remote_handle={'box_id':'persisted'})
            tx.put('queue', job['id'], 'owner', queue)
        _, resumed = jobs.claim(self.repo, 'worker-2')
        self.assertEqual(resumed['attempt'], 1)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('attempt', identity)['data']['started_at'], '2020-01-01T00:00:00Z')

    def test_unknown_and_invalid_timing_is_not_fabricated(self):
        self.assertIsNone(seconds(None)); self.assertIsNone(seconds('invalid'))
        self.assertEqual(seconds(20, 10), 0)

    def test_job_lifecycle_uses_first_attempt_across_retries_for_both_kinds(self):
        with self.repo.transaction() as tx:
            # More than 100 attempt records verifies first starts are not lost
            # when the dashboard's latest 100 jobs have retries.
            for i in range(100):
                kind = 'analysis' if i % 2 else 'paragraph'
                job = dict(id=str(i), kind=kind, status='succeeded', stage='done',
                           created_at='2026-01-01T10:00:00Z', updated_at='2026-01-01T10:05:00Z', completed_at='2026-01-01T10:05:00Z')
                tx.put('job', str(i), 'owner', job)
                tx.put('queue', str(i), 'owner', {'attempt':2})
                tx.put('attempt', f'{i}:1', 'owner', {'started_at':'2026-01-01T10:00:10Z'})
                tx.put('attempt', f'{i}:2', 'owner', {'started_at':'2026-01-01T10:04:00Z'})
        rows = snapshot(self.repo)['jobs']
        self.assertEqual(len(rows), 100)
        for row in rows:
            self.assertEqual(row['started_at'], '2026-01-01T10:00:10Z')
            self.assertEqual(row['attempt_started_at'], '2026-01-01T10:04:00Z')
            self.assertEqual(row['completed_at'], '2026-01-01T10:05:00Z')
            self.assertEqual(row['queue_seconds'], 10)
            self.assertEqual(row['attempt_seconds'], 60)

    def test_queued_and_legacy_jobs_do_not_invent_start_or_finish(self):
        with self.repo.transaction() as tx:
            queued = jobs.enqueue(tx, 'owner', 'analysis')
            legacy = jobs.enqueue(tx, 'owner', 'paragraph')
            legacy.update(status='failed', completed_at=now())
            tx.put('job', legacy['id'], 'owner', legacy)
            tx.put('queue', legacy['id'], 'owner', {'attempt':2})
        rows = {row['id']: row for row in snapshot(self.repo)['jobs']}
        self.assertIsNone(rows[queued['id']]['started_at'])
        self.assertIsNone(rows[queued['id']]['completed_at'])
        self.assertIsNone(rows[legacy['id']]['started_at'])
        self.assertIsNone(rows[legacy['id']]['queue_seconds'])
        self.assertEqual(rows[legacy['id']]['completed_at'], legacy['completed_at'])


SENTINELS = ('PROMPT-TEXT', 'SYSTEM-TEXT', 'QUESTION-TEXT', 'CLAIM-TEXT', 'PACKAGE-TEXT', 'REQUESTER-NAME', '0000-0002-1825-0097',
             'RESPONSE-TEXT', 'DOCUMENT-TEXT', 'REQUESTED-TEXT', 'DEPOSIT-DETAIL')
LIMITS = {'timeout_seconds': 3600, 'max_turns': 999, 'max_budget_usd': 5.0, 'tool_calls': 90}
ACCOUNT_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch']


def account_inputs(claims=3, triples=2):
    return {'format': 'reveal.agent-input/4', 'prompt': 'PROMPT-TEXT ' + 'x' * 88, 'system_prompt': 'SYSTEM-TEXT',
            'tools': list(ACCOUNT_TOOLS), 'graphs': [], 'network': 'open',
            'allowed_domains': [], 'effort': 'medium', 'model': 'claude-opus-5-5', 'limits': dict(LIMITS),
            'deliverable': 'scientific_account', 'claims_repository': True,
            'account': {'question': 'QUESTION-TEXT?', 'attribute_requester': False, 'package': {'note': 'PACKAGE-TEXT'},
                        'claims': [{'statement': f'CLAIM-TEXT {i}', **({'subject_entity': 'HGNC.SYMBOL:LITAF', 'relation':
                                    'biolink:associated_with', 'object_entity': 'KPN.TRAIT:0000505'} if i < triples else {})}
                                   for i in range(claims)]}}


def answer_inputs():
    return {'format': 'reveal.agent-input/4', 'prompt': 'PROMPT-TEXT', 'system_prompt': None, 'tools': ['Read', 'Bash'],
            'graphs': ['kg-one'], 'network': 'open', 'allowed_domains': ['example.org'], 'effort': 'high',
            'model': 'claude-opus-5-5', 'limits': {'timeout_seconds': 600, 'max_turns': 20, 'max_budget_usd': 1.0, 'tool_calls': 30},
            'deliverable': 'answer', 'account': None, 'claims_repository': False}


def account_result(deposit):
    return {'kind': 'agent', 'turns': 49, 'cost_usd': 1.31, 'duration_seconds': 363.5, 'response': 'RESPONSE-TEXT' * 3,
            'response_truncated': False, 'files': [{'path': 'a'}, {'path': 'b'}],
            'account': {'account_id': 'dapper:ScientificAccount.abc', 'objects': 37, 'claims': 3, 'edges': 10, 'value_locators': 6,
                        'lint_warnings': 1, 'dapper_version': '0.2.1-a1', 'document': {'note': 'DOCUMENT-TEXT'},
                        'papers': [{'id': '1'}, {'id': '2'}], 'deposit': deposit,
                        'requested_claims': [{'statement': 'REQUESTED-TEXT', 'assessed': True}]}}


class AgentJobTelemetryTests(AdminTelemetryTests):
    """What kind of agent job was asked for and how it was run, without anything the requester wrote."""

    def agent_job(self, inputs, status='queued', result=None, failure=None, metrics=None, attribution=False, owner='owner'):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, owner, 'agent', inputs=inputs)
            queue = tx.get('queue', job['id'])['data']
            queue['token'] = 'LEASE-TOKEN'
            if attribution: queue['attribution'] = {'user_id': owner, 'display_name': 'REQUESTER-NAME', 'orcid': '0000-0002-1825-0097'}
            tx.put('queue', job['id'], owner, queue)
            if status != 'queued':
                job.update(status=status, stage='complete', result=result, failure=failure, completed_at=now())
                tx.put('job', job['id'], owner, job)
            if metrics:
                tx.put('job_metrics', job['id'], owner, {'format': 'reveal.job-metrics/1', 'kind': 'agent', 'job_id': job['id'],
                                                         'created_at': job['created_at'], 'agent': metrics})
        return job['id']

    def populate(self):
        used = {'model': 'claude-opus-5-5', 'turns': 49, 'turn_limit': 999, 'cost_usd': 1.31, 'budget_usd': 5.0, 'budget_used': .262,
                'cost_source': 'provider', 'tool_calls': 48, 'tool_failures': 7, 'lint_checks': 3, 'draft_writes': 2,
                'time_limit_seconds': 3600, 'status': 'succeeded', 'subtype': 'success'}
        ids = {}
        ids['deposited'] = self.agent_job(account_inputs(), 'succeeded', metrics=used, attribution=True, result=account_result(
            {'status': 'deposited', 'deposit_id': 'dep_1', 'objects': 37, 'stored_differs': 0, 'unresolved_references': 0}))
        ids['undeposited'] = self.agent_job(account_inputs(), 'succeeded', metrics={**used, 'cost_usd': 2.0}, owner='other', result=account_result(
            {'status': 'failed', 'reason': 'rejected', 'http_status': 422, 'detail': 'DEPOSIT-DETAIL'}))
        ids['failed'] = self.agent_job(account_inputs(claims=1, triples=0), 'failed',
                                       failure={'code': 'VALIDATION_FAILED', 'message': 'no', 'retryable': False})
        ids['queued'] = self.agent_job(account_inputs(claims=0))
        ids['answer'] = self.agent_job(answer_inputs(), 'succeeded', metrics={**used, 'turns': 3, 'cost_usd': .1, 'turn_limit': 20},
                                       result={'kind': 'agent', 'turns': 3, 'cost_usd': .1, 'response': 'RESPONSE-TEXT',
                                               'response_truncated': True, 'files': []})
        # A queued answer job with no built-in tools on the restricted network.
        ids['plain'] = self.agent_job({**answer_inputs(), 'tools': [], 'graphs': [], 'network': 'restricted', 'allowed_domains': [],
                                       'limits': {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0, 'tool_calls': 30}})
        with self.repo.transaction() as tx:
            ids['analysis'] = jobs.enqueue(tx, 'owner', 'analysis', inputs={'note': 'PACKAGE-TEXT'})['id']
        return ids

    def test_every_job_row_says_what_an_agent_job_was_asked_to_return(self):
        ids = self.populate()
        data = snapshot(self.repo)
        rows = {row['id']: row for row in data['jobs']}
        self.assertEqual({name: rows[ids[name]]['deliverable'] for name in ids}, {
            'deposited': 'scientific_account', 'undeposited': 'scientific_account', 'failed': 'scientific_account',
            'queued': 'scientific_account', 'answer': 'answer', 'plain': 'answer', 'analysis': None})
        self.assertEqual(data['job_kinds'], [{'kind': 'agent', 'count': 6}, {'kind': 'analysis', 'count': 1}])
        self.assertEqual([rows[ids['deposited']][key] for key in ('tool_calls', 'tool_failures', 'turn_limit', 'draft_writes', 'time_limit_seconds')],
                         [48, 7, 999, 2, 3600])
        self.assertIsNone(rows[ids['queued']]['tool_calls'])

    def test_agent_jobs_report_settings_sizes_and_outcomes(self):
        ids = self.populate()
        block = snapshot(self.repo)['agent_jobs']
        rows = {row['id']: row for row in block['jobs']}
        self.assertEqual(set(rows), {ids[name] for name in ids if name != 'analysis'})
        run = rows[ids['deposited']]['agent']
        self.assertEqual({key: run[key] for key in ('deliverable', 'input_format', 'model', 'effort', 'tools', 'graphs',
                                                    'network', 'allowed_domains', 'claims_repository', 'attribute_requester')},
                         {'deliverable': 'scientific_account', 'input_format': 'reveal.agent-input/4', 'model': 'claude-opus-5-5',
                          'effort': 'medium', 'tools': ACCOUNT_TOOLS, 'graphs': [],
                          'network': 'open', 'allowed_domains': [], 'claims_repository': True, 'attribute_requester': False})
        self.assertNotIn('literature', run)
        self.assertEqual(run['limits'], LIMITS)
        self.assertEqual([run[key] for key in ('prompt_characters', 'system_prompt_characters', 'question_characters',
                                               'claims_requested', 'claims_with_triple')], [100, 11, 14, 3, 2])
        self.assertIsNone(run['output_schema_characters'])                # an account job never asks for structured output
        self.assertEqual(run['result'], {'response_truncated': False, 'account_id': 'dapper:ScientificAccount.abc', 'objects': 37,
                                         'claims': 3, 'quotes': 6, 'lint_warnings': 1, 'dapper_version': '0.2.1-a1',
                                         'deposit_status': 'deposited', 'deposit_id': 'dep_1', 'deposit_reason': None,
                                         'deposit_http_status': None, 'response_characters': 39, 'papers': 2, 'files': 2,
                                         'structured_output_characters': None})
        # A job that succeeded while its deposit did not says so, with the reason and not the repository's message.
        result = rows[ids['undeposited']]['agent']['result']
        self.assertEqual([result[key] for key in ('deposit_status', 'deposit_reason', 'deposit_http_status', 'deposit_id')],
                         ['failed', 'rejected', 422, None])
        # Nothing came back from a failed or a queued job: unknown is None, never 0 or false.
        for name in ('failed', 'queued'):
            self.assertEqual(set(rows[ids[name]]['agent']['result'].values()), {None}, name)
        self.assertEqual([rows[ids['failed']]['agent'][key] for key in ('claims_requested', 'claims_with_triple')], [1, 0])
        self.assertEqual([rows[ids['queued']]['agent'][key] for key in ('claims_requested', 'claims_with_triple')], [0, 0])
        answer = rows[ids['answer']]['agent']
        self.assertEqual([answer[key] for key in ('deliverable', 'claims_repository', 'network', 'allowed_domains', 'graphs',
                                                  'system_prompt_characters', 'question_characters', 'claims_requested', 'attribute_requester')],
                         ['answer', False, 'open', ['example.org'], ['kg-one'], None, None, None, None])
        self.assertEqual([answer['result'][key] for key in ('response_truncated', 'response_characters', 'files', 'papers', 'deposit_status')],
                         [True, 13, 0, None, None])
        plain = rows[ids['plain']]['agent']
        self.assertEqual([plain[key] for key in ('deliverable', 'input_format', 'tools', 'network', 'claims_repository', 'limits')],
                         ['answer', 'reveal.agent-input/4', [], 'restricted', False,
                          {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0, 'tool_calls': 30}])

    def test_claims_frozen_as_plain_statements_count_as_claims_without_a_triple(self):
        inputs = account_inputs()
        inputs['account']['claims'] = ['CLAIM-TEXT one', 'CLAIM-TEXT two']
        job = self.agent_job(inputs)
        run = snapshot(self.repo)['agent_jobs']['jobs'][0]['agent']
        self.assertEqual((run['claims_requested'], run['claims_with_triple']), (2, 0))
        self.assertNotIn('CLAIM-TEXT', json.dumps(snapshot(self.repo)))

    def test_agent_jobs_are_counted_by_what_was_asked_for(self):
        ids = self.populate()
        types = {entry['deliverable']: entry for entry in snapshot(self.repo)['agent_jobs']['types']}
        account = types['scientific_account']
        self.assertEqual({key: account[key] for key in ('jobs', 'succeeded', 'failed', 'cancelled', 'active', 'deposited',
                                                        'deposit_not_made', 'owners', 'spend_usd', 'typical_turns')},
                         {'jobs': 4, 'succeeded': 2, 'failed': 1, 'cancelled': 0, 'active': 1, 'deposited': 1, 'deposit_not_made': 1,
                          'owners': 2, 'spend_usd': 3.31, 'typical_turns': 49})
        self.assertEqual(account['typical_cost_usd'], 2.0)
        answer = types['answer']
        self.assertEqual([answer[key] for key in ('jobs', 'succeeded', 'active', 'deposited', 'deposit_not_made', 'typical_cost_usd')],
                         [2, 1, 1, 0, 0, .1])
        self.assertIsNotNone(account['latest_at'])

    def test_agent_jobs_stay_listed_when_other_jobs_fill_the_job_list(self):
        agent = self.agent_job(answer_inputs())
        with self.repo.transaction() as tx:
            for _ in range(101): jobs.enqueue(tx, 'owner', 'analysis')
        data = snapshot(self.repo)
        self.assertNotIn(agent, {row['id'] for row in data['jobs']})
        self.assertEqual([row['id'] for row in data['agent_jobs']['jobs']], [agent])
        self.assertEqual(data['limits']['agent_jobs'], 100)

    def test_nothing_the_requester_wrote_leaves_the_database(self):
        ids = self.populate()
        from reveal_backend.admin_jobs import job_detail
        encoded = json.dumps(snapshot(self.repo))
        details = {name: job_detail(self.repo, ids[name]) for name in ids}
        encoded += json.dumps(details, default=str)
        for secret in (*SENTINELS, 'LEASE-TOKEN'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(details['deposited']['agent']['claims_with_triple'], 2)
        self.assertEqual(details['deposited']['agent']['result']['deposit_status'], 'deposited')
        self.assertEqual(details['plain']['agent']['deliverable'], 'answer')
        self.assertIsNone(details['analysis']['agent'])

    def test_an_installation_without_agent_jobs_reports_none(self):
        block = snapshot(self.repo)['agent_jobs']
        self.assertEqual((block['jobs'], block['types'], block['window_limit']), ([], [], 100))
