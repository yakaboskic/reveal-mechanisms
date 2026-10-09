"""Per-job cost and failure telemetry: projection, best-effort recording and admin aggregation."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reveal_backend import jobs
from reveal_backend.admin_jobs import job_detail
from reveal_backend.job_metrics import agent_metrics, failure_metrics, record
from reveal_backend.repository import Repository, digest
from reveal_backend.telemetry import cost_summary, snapshot

# The terminal runtime record of a real budget stop, trimmed to the fields the runner writes.
BUDGET_STOP = {
    'model': 'claude-sonnet-4-6',
    'execution_limits': {'max_budget_usd': 3.0, 'max_turns': 100, 'timeout_seconds': 1800},
    'completion': {
        'status': 'failed', 'reason': 'The agent reached its $3 execution budget. No scientific result was accepted.',
        'provider_result_subtype': 'error_max_budget_usd', 'cost_usd': 3.1378407, 'max_budget_usd': 3.0,
        'turns_used': 38, 'turn_limit': 100, 'elapsed_seconds': 1432.71, 'time_limit_seconds': 1800,
        'provider_reported': {'cost_usd': 3.1378407, 'duration_api_ms': 1409722, 'num_turns': 38, 'subtype': 'error_max_budget_usd',
                              'usage': {'input_tokens': 14, 'output_tokens': 70040, 'cache_creation_input_tokens': 246723,
                                        'cache_read_input_tokens': 963242}},
        'timing': {'tools': {'started': 31, 'completed': 30, 'failed': 2}},
        'authoring_timing': {'calls': [{'tool': 'write_account_draft'}, {'tool': 'lint_account'}, {'tool': 'write_account_draft'}, {'tool': 'lint_account'}]},
    },
}


class AgentMetricsTests(unittest.TestCase):
    def test_budget_stop_keeps_provider_numbers_and_counts_repair_rounds(self):
        agent = agent_metrics(BUDGET_STOP)
        self.assertEqual(agent['cost_usd'], 3.1378407); self.assertEqual(agent['cost_source'], 'provider')
        self.assertEqual(agent['budget_usd'], 3.0); self.assertEqual(agent['budget_used'], 1.0459)
        self.assertEqual((agent['turns'], agent['turn_limit']), (38, 100))
        self.assertEqual(agent['tokens'], {'input': 14, 'output': 70040, 'cache_write': 246723, 'cache_read': 963242})
        self.assertEqual(agent['api_seconds'], 1409.722)
        self.assertEqual((agent['tool_calls'], agent['tool_failures']), (30, 2))
        self.assertEqual((agent['lint_checks'], agent['draft_writes']), (2, 2))
        self.assertEqual(agent['subtype'], 'error_max_budget_usd')

    def test_a_killed_run_reports_unknown_cost_never_zero(self):
        agent = agent_metrics({'model': 'claude-sonnet-4-6', 'execution_limits': {'max_budget_usd': 5},
                               'completion': {'status': 'failed', 'cost_usd': None, 'provider_reported': {'usage': None}}})
        self.assertIsNone(agent['cost_usd']); self.assertEqual(agent['cost_source'], 'unreported')
        self.assertIsNone(agent['budget_used']); self.assertIsNone(agent['tokens']); self.assertEqual(agent['budget_usd'], 5)

    def test_untrusted_shapes_are_ignored(self):
        agent = agent_metrics({'completion': {'cost_usd': -1, 'turns_used': 2.5, 'provider_reported': 'text',
                                              'authoring_timing': {'calls': 'none'}}})
        self.assertIsNone(agent['cost_usd']); self.assertIsNone(agent['turns']); self.assertIsNone(agent['lint_checks'])
        self.assertEqual(agent_metrics(None)['cost_source'], 'unreported')

    def test_failure_metrics_bound_and_redact_the_message(self):
        with patch.dict(os.environ, {'PROVIDER_API_KEY': 'sk-very-secret-value'}):
            error = failure_metrics('commit', ValueError('Paragraph assembly changed accepted scientific content sk-very-secret-value ' + 'x' * 900))
        self.assertEqual((error['phase'], error['error_type']), ('commit', 'ValueError'))
        self.assertNotIn('sk-very-secret-value', error['message']); self.assertLessEqual(len(error['message']), 500)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root / 'app.sqlite')); self.repo.migrate()

    def test_agent_and_error_metrics_merge_and_paragraphs_link_their_research_job(self):
        account = 'dapper:ScientificAccount.' + 'a' * 32
        with self.repo.transaction() as tx:
            tx.put('account', digest(['owner', account]), 'owner', {'result': {}, 'summary': {'job_id': 'research-job'}})
            job = jobs.enqueue(tx, 'owner', 'paragraph', account_id=account)
        self.assertTrue(record(self.repo, job, agent=agent_metrics(BUDGET_STOP)))
        self.assertTrue(record(self.repo, job, error=failure_metrics('commit', RuntimeError('boom'))))
        with self.repo.read_transaction() as tx:
            stored = tx.get('job_metrics', job['id'])
        self.assertEqual(stored['owner'], 'owner')
        data = stored['data']
        self.assertEqual((data['kind'], data['parent_job_id']), ('paragraph', 'research-job'))
        self.assertEqual(data['agent']['cost_usd'], 3.1378407); self.assertEqual(data['error']['error_type'], 'RuntimeError')

    def test_recording_never_raises_and_ignores_unknown_jobs(self):
        self.assertFalse(record(self.repo, {'id': 'missing'}, agent={}))
        with patch.object(self.repo, 'transaction', side_effect=OSError('database unavailable')):
            self.assertFalse(record(self.repo, {'id': 'any'}, agent={}))


class CostTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'app.sqlite')); self.repo.migrate()
        self.now = time.time()
        stamp = lambda days: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.now - days * 86400))
        rows = [  # (id, owner, kind, status, failure, days ago, cost, subtype, error)
            ('a1', 'client-user', 'analysis', 'succeeded', None, 0.2, 1.48, 'success', None),
            ('p1', 'client-user', 'paragraph', 'failed', 'VALIDATION_FAILED', 0.1, 0.17, 'success', ('EvidenceBuildError', 'commit')),
            ('a2', 'client-user', 'analysis', 'failed', 'AUTHORING_BUDGET_EXCEEDED', 0.5, 3.14, 'error_max_budget_usd', None),
            ('a3', 'person', 'analysis', 'failed', 'AGENT_EXECUTION_FAILED', 3, None, None, None),
            ('a4', 'person', 'analysis', 'succeeded', None, 20, 1.25, 'success', None),
            ('a5', 'person', 'analysis', 'succeeded', None, 45, 1.36, 'success', None),
            ('a6', 'person', 'analysis', 'cancelled', None, 2, None, None, None),
        ]
        with self.repo.transaction() as tx:
            tx.put('identity', 'client', 'client-user', {'issuer': 'urn:reveal:client:dk', 'subject': 'developer', 'user_id': 'client-user'})
            tx.put('identity', 'person', 'person', {'issuer': 'https://accounts.google.com', 'subject': '1234', 'user_id': 'person'})
            for identity, owner, kind, status, failure, days, cost, subtype, error in rows:
                created = stamp(days)
                tx.put('job', identity, owner, {'id': identity, 'kind': kind, 'status': status, 'stage': 'complete', 'created_at': created,
                                                'updated_at': created, 'completed_at': created, 'failure': {'code': failure} if failure else None})
                metrics = {'format': 'reveal.job-metrics/1', 'job_id': identity, 'kind': kind, 'created_at': created,
                           'agent': {'cost_usd': cost, 'cost_source': 'provider' if cost is not None else 'unreported',
                                     'budget_used': cost / 3 if cost is not None else None, 'subtype': subtype}}
                if identity == 'a6': metrics['agent'].update(estimated_cost_usd=0.9, cost_source='estimated')
                if error: metrics['error'] = {'error_type': error[0], 'phase': error[1]}
                tx.put('job_metrics', identity, owner, metrics)
            tx.put('execution', 'a1', 'client-user', {'created_at': stamp(0.21), 'authoring_attempt': 1, 'recoveries': 2, 'handoffs': 4})

    def test_spend_windows_failures_owners_and_research_distribution(self):
        with self.repo.read_transaction(utc=True) as tx:
            costs = cost_summary(tx, current=self.now)
        self.assertEqual(costs['totals']['day'], {'spend_usd': 4.79, 'jobs': 3, 'estimated': 0, 'unreported': 0, 'failed': 2})
        self.assertEqual(costs['totals']['week'], {'spend_usd': 5.69, 'jobs': 5, 'estimated': 1, 'unreported': 1, 'failed': 3})
        self.assertEqual(costs['totals']['month'], {'spend_usd': 6.94, 'jobs': 6, 'estimated': 1, 'unreported': 1, 'failed': 3})
        self.assertEqual(costs['totals']['window']['jobs'], 7)
        self.assertEqual(costs['research'], {'succeeded': 3, 'p50_usd': 1.36, 'p90_usd': 1.48, 'max_usd': 1.48, 'mean_usd': 1.3633})
        self.assertEqual(costs['budget'], {'exceeded': 1, 'near_limit': 0})
        failures = {f['code']: f for f in costs['failures']}
        self.assertEqual(failures['VALIDATION_FAILED']['causes'], {'EvidenceBuildError · commit': 1})
        self.assertEqual(failures['AUTHORING_BUDGET_EXCEEDED']['causes'], {'error_max_budget_usd': 1})
        self.assertEqual(failures['AUTHORING_BUDGET_EXCEEDED']['spend_usd'], 3.14)
        owners = {o['owner']: o for o in costs['owners']}
        self.assertEqual(owners['client-user']['label'], 'dk/developer'); self.assertIsNone(owners['person']['label'])
        self.assertEqual(owners['client-user']['spend_usd'], 4.79); self.assertEqual(owners['person']['unreported'], 1)
        self.assertEqual(sum(d['jobs'] for d in costs['daily']), 6)

    def test_a_run_that_ended_normally_is_not_named_as_a_failure_cause(self):
        with self.repo.transaction() as tx:
            tx.put('job', 'late', 'person', {'id': 'late', 'kind': 'analysis', 'status': 'failed', 'stage': 'validating',
                                             'created_at': '2026-10-01T10:00:00Z', 'failure': {'code': 'LATE_VALIDATION'}})
            tx.put('job_metrics', 'late', 'person', {'job_id': 'late', 'kind': 'analysis', 'created_at': '2026-10-01T10:00:00Z',
                                                     'agent': {'cost_usd': 0.56, 'cost_source': 'provider', 'subtype': 'success'}})
        with self.repo.read_transaction(utc=True) as tx:
            failures = {f['code']: f for f in cost_summary(tx, current=self.now)['failures']}
        self.assertEqual((failures['LATE_VALIDATION']['jobs'], failures['LATE_VALIDATION']['causes']), (1, {}))

    def test_job_rows_carry_metrics_and_workflow_execution_state(self):
        rows = {row['id']: row for row in snapshot(self.repo)['jobs']}
        self.assertEqual(rows['a2']['cost_usd'], 3.14); self.assertEqual(rows['a3']['cost_source'], 'unreported')
        self.assertEqual((rows['p1']['error_type'], rows['p1']['error_phase']), ('EvidenceBuildError', 'commit'))
        self.assertEqual(rows['a1']['started_at'], rows['a1']['attempt_started_at'])
        self.assertIsNotNone(rows['a1']['started_at']); self.assertEqual((rows['a1']['recoveries'], rows['a1']['handoffs']), (2, 4))
        self.assertIsNone(rows['a4']['started_at'])  # No execution and no attempt rows: never invented.


class WorkflowJobDetailTests(unittest.TestCase):
    def test_workflow_job_detail_shows_metrics_and_its_saved_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = Repository(str(root / 'app.sqlite')); repo.migrate()
            with repo.transaction() as tx:
                job = jobs.enqueue(tx, 'owner', 'paragraph', account_id='dapper:ScientificAccount.' + 'b' * 32)
                tx.put('execution', job['id'], 'owner', {'created_at': job['created_at'], 'authoring_attempt': 1})
                tx.put('job_metrics', job['id'], 'owner', {'job_id': job['id'], 'error': {'error_type': 'EvidenceBuildError', 'phase': 'commit'}})
            failure = root / 'artifacts' / job['id'] / 'attempt-1/failure.json'
            failure.parent.mkdir(parents=True)
            failure.write_text(json.dumps({'phase': 'commit', 'error_type': 'EvidenceBuildError', 'message': 'Paragraph assembly changed accepted scientific content'}))
            with patch.dict(os.environ, {'REVEAL_ARTIFACTS_DIR': str(root / 'artifacts'), 'REVEAL_ARTIFACT_STORE': 'filesystem'}):
                detail = job_detail(repo, job['id'])
        self.assertEqual(detail['metrics']['error']['error_type'], 'EvidenceBuildError')
        self.assertEqual(detail['attempts'], [])
        self.assertIn('attempt-1/failure.json', [item['path'] for item in detail['diagnostics'] if item['available']])


if __name__ == '__main__':
    unittest.main()
