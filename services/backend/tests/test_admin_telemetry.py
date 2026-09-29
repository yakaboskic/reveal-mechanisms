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
