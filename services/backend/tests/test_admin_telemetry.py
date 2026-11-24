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

    def test_owners_are_named_from_their_sign_in_and_anonymous_ones_are_said_to_be(self):
        def person(kind, **profile): return {'me': {**api.fresh_principal(kind, profile), 'private_note': 'keep out'}, 'retired': False}
        ids = {name: uid() for name in ('google', 'key', 'client', 'guest', 'bare')}
        with self.repo.transaction() as tx:
            tx.put('principal', ids['google'], ids['google'], person('registered', display_name='Ada Lovelace', email='ada@example.org'))
            tx.put('principal', ids['key'], ids['key'], person('registered', display_name='Lab colleague (API key)'))
            tx.put('principal', ids['client'], ids['client'], person('registered'))
            tx.put('principal', ids['guest'], ids['guest'], person('anonymous'))
            tx.put('principal', ids['bare'], ids['bare'], person('registered', display_name='  '))
            for owner, issuer, subject in ((ids['google'], 'https://accounts.google.com', '1074960000'),
                                           (ids['key'], 'urn:reveal:operator-api-key', 'api-key-workspace/' + ids['key']),
                                           (ids['client'], 'urn:reveal:client:dk', 'developer')):
                tx.put('identity', uid(), owner, {'user_id': owner, 'issuer': issuer, 'subject': subject})
            for owner in (*ids.values(), 'system-owner'): tx.put('draft', uid(), owner, {'secret': 'private research'})
            job = jobs.enqueue(tx, ids['key'], 'analysis')
        owners = snapshot(self.repo)['owners']
        def entry(kind, name=None, email=None, sign_in=None, api_key=False):
            return {'kind': kind, 'name': name, 'email': email, 'sign_in': sign_in, 'api_key': api_key}
        self.assertEqual(owners, {
            ids['google']: entry('registered', 'Ada Lovelace', 'ada@example.org', 'Google'),
            ids['key']: entry('api_key', 'Lab colleague (API key)'),
            ids['client']: entry('client', 'dk/developer'),
            ids['guest']: entry('anonymous'),
            ids['bare']: entry('registered')})
        # A key configured for a workspace is reported whatever its kind; a workspace that only holds a key and was
        # never registered under a name is a key workspace, not an anonymous guest.
        configured = {'REVEAL_API_KEY_SHA256': 'a' * 64, 'REVEAL_API_KEY_USER_ID': ids['guest'],
                      'REVEAL_API_KEYS': 'b' * 64 + ':' + ids['google']}
        with patch.dict(os.environ, configured):
            keyed = snapshot(self.repo)['owners']
        self.assertEqual(keyed[ids['guest']], entry('api_key', api_key=True))
        self.assertEqual(keyed[ids['google']], entry('registered', 'Ada Lovelace', 'ada@example.org', 'Google', api_key=True))
        self.assertFalse(keyed[ids['key']]['api_key'])
        with patch.dict(os.environ, {'REVEAL_API_KEYS': 'not-a-key-list'}):
            self.assertEqual(snapshot(self.repo)['owners'], owners)
        encoded = json.dumps(owners)
        for hidden in ('1074960000', 'keep out', 'workspace_expires_at', 'system-owner', 'a' * 64): self.assertNotIn(hidden, encoded)
        from reveal_backend.admin_jobs import job_detail
        self.assertEqual(job_detail(self.repo, job['id'])['job']['owner_label'], owners[ids['key']])
        with self.repo.transaction() as tx: other = jobs.enqueue(tx, 'system-owner', 'analysis')
        self.assertIsNone(job_detail(self.repo, other['id'])['job']['owner_label'])

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
             'RESPONSE-TEXT', 'DOCUMENT-TEXT', 'REQUESTED-TEXT', 'DEPOSIT-DETAIL', 'STORED-PATH')
LIMITS = {'timeout_seconds': 3600, 'max_turns': 999, 'max_budget_usd': 5.0, 'tool_calls': 90, 'max_output_tokens': 64000}
ACCOUNT_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch']


def account_inputs(claims=3, triples=2):
    return {'format': 'reveal.agent-input/5', 'prompt': 'PROMPT-TEXT ' + 'x' * 88, 'system_prompt': 'SYSTEM-TEXT',
            'tools': list(ACCOUNT_TOOLS), 'graphs': [], 'network': 'open',
            'allowed_domains': [], 'effort': 'medium', 'model': 'claude-opus-5-5', 'limits': dict(LIMITS),
            'deliverable': 'scientific_account', 'claims_repository': True,
            'account': {'question': 'QUESTION-TEXT?', 'attribute_requester': False, 'package': {'note': 'PACKAGE-TEXT'},
                        'claims': [{'statement': f'CLAIM-TEXT {i}', **({'subject_entity': 'HGNC.SYMBOL:LITAF', 'relation':
                                    'biolink:associated_with', 'object_entity': 'KPN.TRAIT:0000505'} if i < triples else {})}
                                   for i in range(claims)]}}


def answer_inputs():
    return {'format': 'reveal.agent-input/5', 'prompt': 'PROMPT-TEXT', 'system_prompt': None, 'tools': ['Read', 'Bash'],
            'graphs': ['kg-one'], 'network': 'open', 'allowed_domains': ['example.org'], 'effort': 'high',
            'model': 'claude-opus-5-5', 'limits': {'timeout_seconds': 600, 'max_turns': 20, 'max_budget_usd': 1.0, 'tool_calls': 30, 'max_output_tokens': 32000},
            'deliverable': 'answer', 'account': None, 'claims_repository': False}


XML_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML'
# The account's saved sources as the job result lists them; stored_path stands for anything outside the public fields.
PAPERS = [{'path': 'sources/PMC3491803.xml', 'url': XML_URL, 'sha256': 'a' * 64, 'size_bytes': 48213, 'media_type': 'application/xml',
           'command': 'curl -sSL -o output/sources/PMC3491803.xml ' + XML_URL,
           'observed_command': 'mkdir -p output/sources && curl -sSL -o output/sources/PMC3491803.xml ' + XML_URL,
           'derived_from': None, 'evidence_items': 2, 'file_id': 'dapper:File.x', 'stored_path': 'STORED-PATH'},
          {'path': 'sources/lepr.txt', 'url': 'https://example.org/lepr.pdf?api_key=SOURCE-KEY-123', 'sha256': 'b' * 64,
           'size_bytes': 91022, 'media_type': 'text/plain', 'command': 'pdftotext -layout output/sources/lepr.pdf output/sources/lepr.txt',
           'observed_command': 'pdftotext -layout output/sources/lepr.pdf output/sources/lepr.txt', 'derived_from': 'sources/lepr.pdf',
           'evidence_items': 4, 'file_id': 'dapper:File.y'}]


def account_result(deposit):
    return {'kind': 'agent', 'turns': 49, 'cost_usd': 1.31, 'duration_seconds': 363.5, 'response': 'RESPONSE-TEXT' * 3,
            'response_truncated': False, 'files': [{'path': 'a'}, {'path': 'b'}],
            'account': {'account_id': 'dapper:ScientificAccount.abc', 'objects': 37, 'claims': 3, 'edges': 10, 'value_locators': 6,
                        'lint_warnings': 1, 'dapper_version': '0.2.1-a1', 'document': {'note': 'DOCUMENT-TEXT'},
                        'papers': PAPERS, 'deposit': deposit,
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
                                       'limits': {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0, 'tool_calls': 30,
                                                  'max_output_tokens': 32000}})
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
                         {'deliverable': 'scientific_account', 'input_format': 'reveal.agent-input/5', 'model': 'claude-opus-5-5',
                          'effort': 'medium', 'tools': ACCOUNT_TOOLS, 'graphs': [],
                          'network': 'open', 'allowed_domains': [], 'claims_repository': True, 'attribute_requester': False})
        self.assertNotIn('literature', run)
        # Every limit the job was given, the output-token cap included.
        self.assertEqual(run['limits'], LIMITS)
        self.assertEqual(run['limits']['max_output_tokens'], 64000)
        self.assertEqual([run[key] for key in ('prompt_characters', 'system_prompt_characters', 'question_characters',
                                               'claims_requested', 'claims_with_triple')], [100, 11, 14, 3, 2])
        self.assertIsNone(run['output_schema_characters'])                # an account job never asks for structured output
        self.assertEqual(run['result'], {'response_truncated': False, 'account_id': 'dapper:ScientificAccount.abc', 'objects': 37,
                                         'claims': 3, 'quotes': 6, 'lint_warnings': 1, 'dapper_version': '0.2.1-a1',
                                         'deposit_status': 'deposited', 'deposit_id': 'dep_1', 'deposit_reason': None,
                                         'deposit_http_status': None, 'response_characters': 39, 'papers': 2, 'files': 2,
                                         'structured_output_characters': None,
                                         **dict.fromkeys(('screen_outcome', 'screen_check', 'screen_score', 'screen_threshold',
                                                          'screen_location', 'screen_classifier', 'screen_windows', 'screen_texts',
                                                          'screen_seconds'))})
        # The job list carries no source paths, URLs or commands: only the job detail does.
        self.assertNotIn('sources', run)
        self.assertNotIn(XML_URL, json.dumps(block))
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
                         ['answer', 'reveal.agent-input/5', [], 'restricted', False,
                          {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0, 'tool_calls': 30, 'max_output_tokens': 32000}])

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

    def test_a_withheld_account_reports_the_screens_check_score_threshold_and_location(self):
        from reveal_backend import job_metrics
        ids = self.populate()
        withheld = self.agent_job(account_inputs(), 'succeeded', result=account_result(
            {'status': 'withheld_unsafe', 'reason': 'prompt_injection', 'detail': 'DEPOSIT-DETAIL'}))
        passed = ids['deposited']
        verdicts = {withheld: {'outcome': 'withheld', 'check': 'prompt_injection', 'location': 'sources/PMC1.txt line=12-14',
                               'score': 0.9731, 'threshold': 0.5, 'classifier': 'prompt-guard-2-22m@0123456789ab', 'windows': 84,
                               'texts': 9, 'seconds': 2.4},
                    passed: {'outcome': 'passed', 'check': None, 'location': 'ledger call 7 (WebFetch) line=1-3', 'score': 0.012,
                             'threshold': 0.5, 'classifier': 'prompt-guard-2-22m@0123456789ab', 'windows': 31, 'texts': 6, 'seconds': 0.9}}
        for identity, verdict in verdicts.items():
            self.assertTrue(job_metrics.record(self.repo, {'id': identity}, screen=verdict))
        block = snapshot(self.repo)['agent_jobs']
        rows = {row['id']: row['agent']['result'] for row in block['jobs']}
        self.assertEqual({key: rows[withheld][key] for key in ('deposit_status', 'deposit_reason', 'screen_outcome', 'screen_check', 'screen_score',
                                                               'screen_threshold', 'screen_location', 'screen_windows', 'screen_texts',
                                                               'screen_seconds')},
                         {'deposit_status': 'withheld_unsafe', 'deposit_reason': 'prompt_injection', 'screen_outcome': 'withheld',
                          'screen_check': 'prompt_injection', 'screen_score': 0.9731, 'screen_threshold': 0.5,
                          'screen_location': 'sources/PMC1.txt line=12-14', 'screen_windows': 84, 'screen_texts': 9, 'screen_seconds': 2.4})
        self.assertEqual((rows[passed]['screen_outcome'], rows[passed]['screen_score'], rows[passed]['deposit_status']), ('passed', 0.012, 'deposited'))
        # Counted on its own, by check, and not among the deposits that failed.
        account = {entry['deliverable']: entry for entry in block['types']}['scientific_account']
        self.assertEqual((account['withheld_unsafe'], account['withheld_by_check'], account['deposit_not_made'], account['deposited']),
                         (1, {'prompt_injection': 1}, 1, 1))
        self.assertNotIn('DEPOSIT-DETAIL', json.dumps(snapshot(self.repo)))

    def test_a_screen_run_with_the_classifier_disabled_says_so(self):
        from reveal_backend import job_metrics
        ids = self.populate()
        # The classifier's lock is not pinned: only the fixed checks ran, and the verdict names the classifier disabled.
        self.assertTrue(job_metrics.record(self.repo, {'id': ids['deposited']}, screen={
            'outcome': 'passed', 'check': None, 'location': None, 'score': None, 'threshold': 0.5, 'classifier': 'disabled',
            'windows': 0, 'texts': 0, 'seconds': 0.1}))
        row = {row['id']: row['agent']['result'] for row in snapshot(self.repo)['agent_jobs']['jobs']}[ids['deposited']]
        self.assertEqual({key: row[key] for key in ('screen_outcome', 'screen_classifier', 'screen_score', 'screen_windows')},
                         {'screen_outcome': 'passed', 'screen_classifier': 'disabled', 'screen_score': None, 'screen_windows': 0})

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

    def test_only_the_admin_content_read_returns_what_the_requester_wrote(self):
        """c7486dd's sizes-only rule, reversed for GET /internal/v1/admin/jobs/{id}/run alone: the 15-second snapshot and
        the job detail still select no content, and the content read returns it."""
        from reveal_backend import telemetry
        from reveal_backend.admin_run import job_run
        # The snapshot projects settings by allowlisted path; no content path is among them.
        content = {'inputs.prompt', 'inputs.system_prompt', 'inputs.account', 'inputs.account.question', 'inputs.account.claims',
                   'inputs.output_schema', 'result.response', 'result.structured_output', 'result.account.document'}
        self.assertFalse(content & (set(telemetry.AGENT_INPUT_FIELDS) | set(telemetry.AGENT_RESULT_FIELDS)))
        ids = self.populate()
        with patch.dict(os.environ, {'REVEAL_ARTIFACTS_DIR': str(Path(self.repo.sqlite_path).parent / 'artifacts')}):
            view = job_run(self.repo, ids['deposited'], 'admin@example.org')
        encoded = json.dumps(view)
        for text in ('PROMPT-TEXT', 'SYSTEM-TEXT', 'QUESTION-TEXT', 'CLAIM-TEXT', 'RESPONSE-TEXT'):
            self.assertIn(text, encoded)
        # Even this read never returns the lease, who the account credits, the evidence package or the account document.
        for text in ('LEASE-TOKEN', 'REQUESTER-NAME', '0000-0002-1825-0097', 'PACKAGE-TEXT', 'DOCUMENT-TEXT', 'DEPOSIT-DETAIL'):
            self.assertNotIn(text, encoded)
        self.assertNotIn('PROMPT-TEXT', json.dumps(snapshot(self.repo)))

    def test_the_job_detail_lists_each_saved_source_by_its_metadata(self):
        ids = self.populate()
        from reveal_backend.admin_jobs import job_detail
        sources = job_detail(self.repo, ids['deposited'])['agent']['sources']
        self.assertEqual(sources['total'], 2)
        self.assertEqual(sources['items'][0], {key: PAPERS[0][key] for key in ('path', 'url', 'command', 'observed_command', 'derived_from',
                                                                                 'sha256', 'media_type', 'size_bytes', 'evidence_items')})
        # Only the listed fields; a credential the agent put in a URL is redacted like any diagnostic.
        self.assertEqual(sources['items'][1]['url'], 'https://example.org/lepr.pdf?api_key=[redacted]')
        self.assertEqual((sources['items'][1]['derived_from'], sources['items'][1]['evidence_items']), ('sources/lepr.pdf', 4))
        encoded = json.dumps(sources)
        for hidden in ('STORED-PATH', 'SOURCE-KEY-123', 'dapper:File.x'):
            self.assertNotIn(hidden, encoded)
        # A job with no account has no sources; an answer job is never asked.
        self.assertIsNone(job_detail(self.repo, ids['failed'])['agent']['sources'])
        self.assertNotIn('sources', job_detail(self.repo, ids['answer'])['agent'])

    def test_the_job_detail_bounds_the_source_list_and_each_field(self):
        many = [{**PAPERS[0], 'path': f'sources/p{index}.xml', 'command': 'curl ' + 'x' * 5000, 'size_bytes': True}
                for index in range(130)]
        identity = self.agent_job(account_inputs(), 'succeeded', result={**account_result({'status': 'deposited'}),
                                  'account': {**account_result({'status': 'deposited'})['account'], 'papers': many}})
        from reveal_backend.admin_jobs import job_detail
        sources = job_detail(self.repo, identity)['agent']['sources']
        self.assertEqual((sources['total'], len(sources['items'])), (130, 100))
        self.assertEqual(len(sources['items'][0]['command']), 2000)
        self.assertIsNone(sources['items'][0]['size_bytes'])   # a boolean is not a size

    def test_an_installation_without_agent_jobs_reports_none(self):
        block = snapshot(self.repo)['agent_jobs']
        self.assertEqual((block['jobs'], block['types'], block['window_limit']), ([], [], 100))
