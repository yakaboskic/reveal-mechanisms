"""Submission latency must not monopolize the API event loop or duplicate jobs."""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import jwt

from reveal_backend import app as api
from reveal_backend.repository import Repository, now, uid


class CachedCatalog:
    dismech_import, mapping_run, embedding_run = 'source-run', 'mapping-run', 'embedding-run'
    gaps, factors = {'gap': {}}, {'factor:portal:test:model:Factor1': {}}

    def load(self): pass

    def selected(self, reference): return reference


class PausableRepository(Repository):
    def __init__(self, path):
        super().__init__(path)
        self.pause = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.submission_thread = None

    @contextmanager
    def transaction(self):
        if self.pause:
            self.submission_thread = threading.get_ident()
            self.entered.set()
            if not self.release.wait(2):
                raise RuntimeError('Test submission was not released')
        with super().transaction() as tx:
            yield tx


class JobSubmissionResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = PausableRepository(str(Path(self.temp.name) / 'app.sqlite'))
        self.repo.migrate()
        self.owner, self.other = uid(), uid()
        self.draft_id = uid()
        self.body = {'kind': 'analysis', 'draft_id': self.draft_id, 'draft_version': 1}
        gap = {'id': 'dapper:KnowledgeGap.' + 'g' * 32, 'text': 'Test source question'}
        factor = {'source_id': 'factor:portal:test:model:Factor1'}
        composer = {'source_gap': {'source_id': 'dismech:test-gap'},
                    'eaggl_anchors': [{'reference': factor}]}
        with self.repo.transaction() as tx:
            for owner in (self.owner, self.other):
                me = dict(api.fresh_principal('registered'), user_id=owner)
                tx.put('principal', owner, owner, {'me': me})
            tx.put('draft', self.draft_id, self.owner, {
                'id': self.draft_id, 'version': 1, 'composer': composer,
                'created_at': now(), 'updated_at': now()})
            tx.put('draft_binding', self.draft_id, self.owner, {
                'dismech_import_id': 'source-run', 'source_gap': {'object': gap, 'attachments': []},
                'selections': {factor['source_id']: {
                    'record': {'object': {'id': 'dapper:Mechanism.' + 'm' * 32, 'name': 'Test factor'}},
                    'binding': {'source_id': factor['source_id']}}}})
        for target in (patch.object(api, 'repo', self.repo), patch.object(api, 'catalog', CachedCatalog()),
                       patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40,
                           'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api'})):
            target.start()
            self.addCleanup(target.stop)

    def headers(self, key, owner=None):
        timestamp = int(time.time())
        token = jwt.encode({'sub': owner or self.owner, 'principal_kind': 'registered',
            'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': timestamp,
            'exp': timestamp + 120, 'jti': uid()}, 's' * 40, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': key}

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test')

    async def test_slow_submission_preserves_event_loop_and_database_readiness(self):
        self.repo.pause = True
        async with self.client() as client:
            submission = asyncio.create_task(client.post('/v1/jobs', json=self.body, headers=self.headers(uid())))
            try:
                for _ in range(40):
                    if self.repo.entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(self.repo.entered.is_set())
                self.assertNotEqual(self.repo.submission_thread, threading.get_ident())
                self.assertFalse(submission.done())
                ready = await asyncio.wait_for(client.get('/readyz'), timeout=.5)
                self.assertEqual(ready.status_code, 200, ready.text)
                self.assertEqual(ready.json()['status'], 'ready')
                self.assertFalse(submission.done())
            finally:
                self.repo.release.set()
                result = await submission
        self.assertEqual(result.status_code, 202, result.text)

    async def test_overlapping_retries_create_exactly_one_request_job_and_event(self):
        headers = self.headers(uid())
        async with self.client() as client:
            first, replay = await asyncio.gather(
                client.post('/v1/jobs', json=self.body, headers=headers),
                client.post('/v1/jobs', json=self.body, headers=headers))
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(first.json(), replay.json())
            conflict = await client.post('/v1/jobs', json=dict(self.body, draft_version=2), headers=headers)
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()['code'], 'IDEMPOTENCY_CONFLICT')
        with self.repo.read_transaction() as tx:
            for kind in ('request', 'request_binding', 'queue', 'event', 'job', 'idempotency'):
                self.assertEqual(len(tx.list(kind, self.owner)), 1, kind)
            self.assertEqual(tx.list('job', self.owner)[0]['data']['id'], first.json()['id'])

    async def test_threaded_submission_preserves_ownership_and_rolls_back_errors(self):
        async with self.client() as client:
            missing = await client.post('/v1/jobs', json=self.body, headers=self.headers(uid(), self.other))
            self.assertEqual(missing.status_code, 404, missing.text)
            conflict = await client.post('/v1/jobs', json=dict(self.body, draft_version=2), headers=self.headers(uid()))
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()['code'], 'VERSION_CONFLICT')
        with self.repo.read_transaction() as tx:
            for kind in ('request', 'request_binding', 'queue', 'event', 'job', 'idempotency'):
                self.assertEqual(tx.list(kind), [], kind)


if __name__ == '__main__':
    unittest.main()
