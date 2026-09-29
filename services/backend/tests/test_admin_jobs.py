"""Job log authorization, indexed history, saved diagnostics and credential isolation."""
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
from reveal_backend.admin_jobs import job_detail, MAX_DIAGNOSTIC_BYTES
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, uid


class AdminJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = Repository(str(self.root/'app.sqlite')); self.repo.migrate()
        for p in (patch.object(api, 'repo', self.repo), patch.dict(os.environ, {
            'REVEAL_ARTIFACTS_DIR': str(self.root/'artifacts'), 'REVEAL_GATEWAY_SECRET':'s'*40,
            'REVEAL_GATEWAY_SERVICE_TOKEN':'t'*40, 'REVEAL_GATEWAY_ISSUER':'reveal-nextjs',
            'REVEAL_GATEWAY_AUDIENCE':'reveal-api', 'DISABLE_ADMIN_LOGIN':'true', 'ANTHROPIC_API_KEY':'secret-provider-credential'})):
            p.start(); self.addCleanup(p.stop)
        with self.repo.transaction() as tx: self.job = jobs.enqueue(tx, 'owner-1', 'analysis')
        _, self.queue = jobs.claim(self.repo, 'worker-1')
        self.directory = self.root/'artifacts'/self.job['id']
        (self.directory/'attempt-1').mkdir(parents=True)
        self.client = TestClient(api.app)
        self.url = '/internal/v1/admin/jobs/'+self.job['id']

    def headers(self, purpose='admin_telemetry'):
        proof = jwt.encode({'sub':'admin-console', 'iss':'reveal-nextjs', 'aud':'reveal-api',
            'iat':int(time.time()), 'exp':int(time.time())+120, 'jti':uid(), 'purpose':purpose}, 's'*40, algorithm='HS256')
        return {'Authorization':'Bearer '+'t'*40, 'X-Reveal-Admin-Assertion':proof}

    def test_admin_auth_is_required_for_job_logs_and_files(self):
        for headers, status in [({},403), ({'Authorization':'Bearer '+'t'*40},401), (self.headers('verified_identity'),401)]:
            self.assertEqual(self.client.get(self.url, headers=headers).status_code, status)
        response = self.client.get(self.url, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(response.json()['job']['id'], self.job['id'])
        self.assertNotIn(self.queue['token'], response.text)
        self.assertEqual(self.client.post(self.url, headers=self.headers()).status_code, 405)

    def test_history_starts_at_tail_and_pages_without_missing_or_cross_job_events(self):
        with self.repo.transaction() as tx:
            current = tx.get('job', self.job['id'])['data']
            for i in range(205):
                jobs.event(tx, current, 'activity', f'Log {i}', {'kind':'tool_result','tool_name':'query_graph','display_arguments':'{"gene":"IL6"}','output_excerpt':'Graph response','state':'completed'})
            other = jobs.enqueue(tx, 'owner-2', 'paragraph')
            jobs.event(tx, other, 'activity', 'Unrelated private message')
        jobs.finish(self.repo, self.job['id'], self.queue['token'], 'failed', failure={'code':'TEST_FAILURE','message':'Terminal reason','retryable':True})
        page = job_detail(self.repo, self.job['id'])
        self.assertEqual(len(page['events']), 100)
        self.assertEqual(page['events'][-1]['message'], 'Terminal reason')
        self.assertEqual(page['events'][0]['detail']['tool_name'], 'query_graph')
        ids = [int(e['id']) for e in page['events']]
        self.assertEqual(ids, sorted(ids))
        while page['next_before']:
            page = job_detail(self.repo, self.job['id'], before=page['next_before'])
            self.assertNotIn('Unrelated private message', json.dumps(page))
            ids = [int(e['id']) for e in page['events']] + ids
        self.assertEqual(ids, list(range(1, 209)))

    def test_saved_failure_and_budget_report_are_exposed_only_as_redacted_diagnostics(self):
        failure = {'phase':'evidence_preparation','error_type':'EvidenceBuildError',
                   'message':'41092 > 24000; secret-provider-credential Bearer provider-token',
                   'api_key':'different-secret', 'nested':{'password':'also-secret'}}
        (self.directory/'attempt-1/failure.json').write_text(json.dumps(failure))
        (self.directory/'evidence').mkdir()
        (self.directory/'evidence/dispatch-budget-failure.json').write_text(json.dumps({'budget':24000,'attempts':[{'input_tokens':41092}]}))
        (self.directory/'attempt-1/credentials.json').write_text('{"secret":"never-expose"}')
        jobs.finish(self.repo, self.job['id'], self.queue['token'], 'failed', failure={'code':'EVIDENCE_PREPARATION_FAILED','message':'Evidence preparation failed.','retryable':True})
        detail = job_detail(self.repo, self.job['id'])
        self.assertEqual(len(detail['diagnostics']), 2)
        encoded = json.dumps(detail)
        self.assertIn('41092 > 24000', encoded)
        for secret in ('secret-provider-credential','provider-token','different-secret','also-secret','never-expose',self.queue['token']):
            self.assertNotIn(secret, encoded)
        self.assertIn('[redacted]', encoded)
        with self.repo.read_transaction() as tx:
            self.assertNotIn('41092', json.dumps(tx.get('job', self.job['id'])['data']))

    def test_missing_oversized_partial_and_symlinked_diagnostics_are_safe(self):
        self.assertEqual(job_detail(self.repo, self.job['id'])['diagnostics'], [])
        (self.directory/'attempt-1/failure.json').write_text('{')
        (self.directory/'attempt-1/token-budget.json').write_text('x'*(MAX_DIAGNOSTIC_BYTES+1))
        outside = self.root/'outside.json'; outside.write_text('{"message":"outside-secret"}')
        (self.directory/'attempt-1/paragraph-grounding.json').symlink_to(outside)
        (self.directory/'evidence').symlink_to(self.root, target_is_directory=True)
        (self.root/'dispatch-budget-failure.json').write_text('{"message":"cross-job-secret"}')
        detail = job_detail(self.repo, self.job['id'])
        self.assertEqual(len(detail['diagnostics']), 2)
        self.assertTrue(all(not d['available'] for d in detail['diagnostics']))
        self.assertNotIn('outside-secret', json.dumps(detail))
        self.assertNotIn('cross-job-secret', json.dumps(detail))

    def test_invalid_ids_cursors_and_missing_jobs_are_explicit(self):
        for job_id in ('../etc/passwd', 'arbitrary', uid()):
            with self.assertRaises(Problem) as caught: job_detail(self.repo, job_id)
            self.assertEqual(caught.exception.status, 404)
        for params in ({'before':'-1'}, {'before':'0'}, {'before':'bad'}, {'before':'9'*13}, {'limit':0}, {'limit':101}):
            self.assertEqual(self.client.get(self.url, params=params, headers=self.headers()).status_code, 422)

    def test_direct_job_lookup_is_not_limited_to_dashboard_recent_jobs(self):
        with self.repo.transaction() as tx:
            for _ in range(101): jobs.enqueue(tx, 'owner-2', 'paragraph')
        detail = job_detail(self.repo, self.job['id'])
        self.assertEqual(detail['job']['id'], self.job['id'])
        self.assertEqual(detail['attempts'][0]['worker_id'], 'worker-1')
