"""Durable import steps must survive replay, retain gates, and reject outsiders."""
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reveal_backend.repository import Repository
from reveal_backend.vector_ingestion import VectorRegistry, import_batch, quality_baseline, save_export
from reveal_backend.vector_workflow import dispatch_pending, enroll, finalize, inventory_page, mount_vector_workflow, plan, query_probe, PATH
from reveal_backend.workflow_routes import pending
from test_vector_retrieval import Provider, fixture


class VectorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'local', 'REVEAL_ARTIFACT_STORE': 'filesystem',
            'REVEAL_JOB_NAMESPACE': 'vector-test',
            'REVEAL_ENVIRONMENT': 'test', 'QSTASH_TOKEN': 'test', 'QSTASH_CURRENT_SIGNING_KEY': 'test',
            'QSTASH_NEXT_SIGNING_KEY': 'next', 'REVEAL_WORKFLOW_URL': 'https://workflow.invalid/internal/workflows/research-v1'})
        self.env.start()
        self.repo = Repository(Path(self.directory.name) / 'test.sqlite3'); self.repo.migrate()
        self.registry = VectorRegistry(self.repo)
        manifest, populated, _ = fixture(); manifest.update(status='loading', export_ref={}, verified_batches={})
        rows = []
        for kind, ns in [('factors', manifest['factor_namespace']), ('contexts', manifest['context_namespace'])]:
            manifest['batches'][kind + ':0'] = [row['id'] for row in manifest[kind]]
            rows.extend({'kind': kind, **row, 'vector': populated.rows[ns][row['id']]['vector']} for row in manifest[kind])
        manifest['quality_probes'] = quality_baseline(manifest, rows)
        self.registry.register(save_export(manifest, rows, self.directory.name))
        self.identity, self.provider = manifest['snapshot_id'], Provider()
    def tearDown(self): self.env.stop(); self.directory.cleanup()

    def test_replayed_bounded_steps_and_finalization(self):
        enroll(self.registry, self.identity, activate=True)
        details = plan(self.registry, {'snapshot_id': self.identity, 'environment': 'local', 'namespace': 'vector-test'})
        self.assertEqual(details['batches'], ['contexts:0', 'factors:0'])
        for key in details['batches']: import_batch(self.registry, self.identity, key, client=self.provider)
        with self.assertRaises(ValueError): finalize(self.registry, self.identity, client=self.provider)
        for kind in ('factors', 'contexts'):
            first = inventory_page(self.registry, self.identity, kind, client=self.provider)
            self.assertEqual(first, inventory_page(self.registry, self.identity, kind, client=self.provider))
        for i in range(details['probes']):
            first = query_probe(self.registry, self.identity, i, client=self.provider)
            self.assertEqual(first, query_probe(self.registry, self.identity, i, client=self.provider))
        report = finalize(self.registry, self.identity, client=self.provider)
        self.assertEqual(report['minimum_recall_at_10'], 1)
        self.registry.activate(self.identity)
        self.assertEqual(self.registry.active()['snapshot_id'], self.identity)

    def test_plan_requires_durable_intent_and_environment(self):
        payload = {'snapshot_id': self.identity, 'environment': 'local', 'namespace': 'vector-test'}
        with self.assertRaises(ValueError): plan(self.registry, payload)
        enroll(self.registry, self.identity)
        with self.assertRaises(ValueError): plan(self.registry, dict(payload, environment='production'))
        with self.assertRaises(ValueError): plan(self.registry, dict(payload, namespace='other-app'))
        with self.assertRaises(ValueError): enroll(self.registry, self.identity, activate=True)

    def test_enrollment_is_selected_by_real_pending_and_dispatched_once(self):
        enroll(self.registry, self.identity)
        self.assertEqual(len(pending(self.repo, 'vector_dispatch', 10)), 1)
        with patch.dict('os.environ', {'REVEAL_JOB_NAMESPACE': 'other-app'}):
            self.assertEqual(pending(self.repo, 'vector_dispatch', 10), [])
            with self.assertRaises(ValueError): enroll(self.registry, self.identity)
        http = SimpleNamespace(request=AsyncMock(return_value=[]))
        with patch('reveal_backend.workflow_routes.client', return_value=SimpleNamespace(http=http)):
            self.assertEqual(asyncio.run(dispatch_pending(self.repo)), {'delivered': 1, 'failed': 0})
            self.assertEqual(asyncio.run(dispatch_pending(self.repo)), {'delivered': 0, 'failed': 0})
        http.request.assert_awaited_once()
        batch = json.loads(http.request.call_args.kwargs['body'])
        payload = json.loads(batch[0]['body'])
        self.assertEqual(payload, {'snapshot_id': self.identity, 'environment': 'local', 'namespace': 'vector-test'})
        self.assertEqual(pending(self.repo, 'vector_dispatch', 10), [])
        self.assertTrue(plan(self.registry, payload)['batches'])

    def test_explicit_reenrollment_upgrades_legacy_namespace(self):
        enroll(self.registry, self.identity)
        with self.repo.transaction() as tx:
            for kind in ('vector_dispatch', 'vector_import_request'):
                row = tx.get(kind, self.identity)['data']; row.pop('namespace')
                tx.put(kind, self.identity, 'catalog', row)
        self.assertEqual(pending(self.repo, 'vector_dispatch', 10), [])
        enroll(self.registry, self.identity)
        self.assertEqual(pending(self.repo, 'vector_dispatch', 10)[0][1]['namespace'], 'vector-test')
        self.assertTrue(plan(self.registry, {'snapshot_id': self.identity, 'environment': 'local', 'namespace': 'vector-test'}))

    def test_unsigned_workflow_request_cannot_write(self):
        app = FastAPI(); mount_vector_workflow(app, self.repo)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(PATH, json={'snapshot_id': self.identity, 'environment': 'local'})
        self.assertEqual(response.status_code, 500)  # Pinned SDK rejects signatures as WorkflowError.
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('vector_batch'), [])
            self.assertEqual(tx.list('vector_active'), [])


if __name__ == '__main__': unittest.main()
