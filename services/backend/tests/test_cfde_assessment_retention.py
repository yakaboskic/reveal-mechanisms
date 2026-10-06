"""Assessments survive reference reloads privately and transfer without cache authority."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import cfde_assessment_cache as cache, user_inputs, workspace_events
from reveal_backend import reference_archive as archive
from reveal_backend.auth import Problem, owned
from reveal_backend.repository import Repository, digest, uid
import test_cfde_assessment_cache as cache_fixture
import test_reference_archive as reference_fixture

KINDS = ('cfde_assessment', 'cfde_assessment_cache', 'cfde_assessment_idempotency',
         'cfde_assessment_shared', 'cfde_assessment_shared_cache')


class AssessmentRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'retention.sqlite'); self.repo.migrate()
        notification = patch('reveal_backend.redis_notifications.publish')
        notification.start(); self.addCleanup(notification.stop)
        self.source, self.target = 'anonymous-owner', 'registered-owner'

    def test_all_assessment_kinds_are_retained_and_never_broadcast(self):
        with self.repo.transaction() as tx:
            for kind in KINDS:
                self.assertEqual(archive.classify(kind), archive.KEEP)
                self.assertFalse(workspace_events.tracked(kind))
                tx.put(kind, uid(), cache.OWNER if kind.startswith('cfde_assessment_shared') else self.source,
                       {'private_scientific_snapshot': 'must not enter workspace notifications'})
            self.assertEqual(tx.workspace_changes, {})
            self.assertEqual(tx.notification_jobs, set())
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('workspace_event'), [])
            self.assertEqual(tx.list('notification_outbox'), [])

    def test_reference_cutover_preserves_exact_private_and_shared_history(self):
        fixture = reference_fixture.Repo(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        drafted = fixture.draft(fixture.owner, [reference_fixture.LEGACY_ANCHOR])
        identities = {}
        with fixture.repo.transaction() as tx:
            for kind in KINDS:
                identities[kind] = uid()
                data = {'public': {'draft_id': drafted[0], 'reference_generation_id': reference_fixture.LEGACY},
                        'original_snapshot': {'source_revision': '9' * 64}}
                if kind == 'cfde_assessment': data['inputs'] = {'context': 'Private immutable hypothesis'}
                tx.put(kind, identities[kind], cache.OWNER if kind.startswith('cfde_assessment_shared') else fixture.owner, data)
        before = {kind: fixture.get(kind, identity) for kind, identity in identities.items()}
        plan = archive.plan_prefix(fixture.repo, reference_fixture.KPN, from_generation=reference_fixture.LEGACY)
        self.assertTrue(plan['ok']); self.assertEqual(plan['unknown_kinds'], [])
        self.assertTrue(all(plan['actions'][kind] == 'keep' for kind in KINDS))
        fixture.activate()
        archive.archive_prefix(fixture.repo, reference_fixture.LEGACY, reference_fixture.KPN, apply=True,
                               at=reference_fixture.T1)
        self.assertIsNone(fixture.get('draft', drafted[0]))
        for kind, identity in identities.items(): self.assertEqual(fixture.get(kind, identity), before[kind])

    def test_new_reference_generation_cannot_reuse_old_shared_result(self):
        old_key = cache_fixture.key(generation='a' * 64); new_key = cache_fixture.key(generation='b' * 64)
        value = cache_fixture.public('succeeded')
        with self.repo.transaction() as tx: reference = cache.create_shared(tx, old_key, value)
        with patch.object(cache, 'now', return_value=cache_fixture.iso(cache_fixture.STAMP)):
            with self.repo.read_transaction() as tx:
                self.assertIsNotNone(cache.read_shared(tx, old_key)); self.assertIsNone(cache.read_shared(tx, new_key))
                historical = cache.projection_fields(tx, {'shared_ref': reference})
        self.assertEqual(historical['reference_generation_id'], 'a' * 64)
        self.assertEqual(historical['result'], value['result'])

    def test_transfer_fences_only_private_pending_workers_and_leaves_shared_service_state(self):
        finished, pending_private, pending_public, follower = uid(), uid(), uid(), uid()
        finished_data = {'public': {**cache_fixture.public('succeeded', identity=finished), 'draft_id': 'draft'},
            'composer': {'context': 'Private accepted question'}, 'inputs': {'context': 'Private accepted question'},
            'request': {'source_generation': 'a' * 64}, 'response_sha256': 'f' * 64}
        private_data = {'public': cache_fixture.public(identity=pending_private),
                        'composer': {'context': 'Private hypothesis'}, 'inputs': {'context': 'Private hypothesis'}}
        public_data = {'public': cache_fixture.public(identity=pending_public),
                       'composer': {'context': ' \n'}, 'inputs': {'context': ' \n'}}
        with self.repo.transaction() as tx:
            tx.put('draft', 'draft', self.source, {'id': 'draft', 'composer': {}})
            shared_ref = cache.create_shared(tx, cache_fixture.key(), public_data['public'])
            public_data['shared_ref'] = shared_ref
            follower_data = {'public': cache_fixture.public(identity=follower), 'composer': {}, 'inputs': {}, 'shared_ref': shared_ref}
            for identity, data in [(finished, finished_data), (pending_private, private_data),
                                   (pending_public, public_data), (follower, follower_data)]:
                tx.put('cfde_assessment', identity, self.source, data)
            tx.put('cfde_assessment_cache', 'old-owner-cache', self.source, {'id': finished})
            tx.put('cfde_assessment_idempotency', 'old-owner-key', self.source, {'assessment_id': finished})
            tx.put('cfde_assessment_cache', 'target-cache', self.target, {'id': 'target-assessment'})
            # A malformed shared row cannot gain a new personal owner through
            # the broad fallback transfer SQL, either.
            tx.put(cache.KIND, 'misowned-shared', self.source, {'test': 'unchanged'})
            shared_before = tx.get(cache.KIND, digest([shared_ref['key'], pending_public]))
            index_before = tx.get(cache.INDEX_KIND, shared_ref['key'])
            tx.transfer(self.source, self.target)
            self.assertEqual(tx.get('cfde_assessment', finished)['data'], finished_data)
            self.assertEqual(tx.get('cfde_assessment', finished)['owner'], self.target)
            private = tx.get('cfde_assessment', pending_private)
            self.assertEqual(private['owner'], self.target)
            self.assertEqual(private['data']['public']['status'], 'interrupted')
            self.assertEqual(private['data']['public']['error']['code'], 'WORKSPACE_TRANSFERRED')
            self.assertEqual(private['data']['inputs'], private_data['inputs'])
            self.assertEqual(tx.get('cfde_assessment', pending_public)['data'], public_data)
            self.assertEqual(tx.get('cfde_assessment', follower)['data'], follower_data)
            self.assertEqual(tx.get(cache.KIND, digest([shared_ref['key'], pending_public])), shared_before)
            self.assertEqual(tx.get(cache.INDEX_KIND, shared_ref['key']), index_before)
            self.assertEqual(tx.get(cache.KIND, 'misowned-shared')['owner'], self.source)
            self.assertIsNone(tx.get('cfde_assessment_cache', 'old-owner-cache'))
            self.assertIsNone(tx.get('cfde_assessment_idempotency', 'old-owner-key'))
            self.assertEqual(tx.get('cfde_assessment_cache', 'target-cache')['owner'], self.target)
            with self.assertRaises(Problem): owned(tx, 'cfde_assessment', pending_private, self.source)

    def test_upload_only_private_pending_work_is_interrupted_on_transfer(self):
        identity = uid()
        data = {'public': cache_fixture.public(identity=identity), 'composer': {'upload_ids': ['upload']},
                'inputs': {'uploads': [{'id': 'upload', 'sha256': '1' * 64}]}}
        with self.repo.transaction() as tx:
            tx.put('cfde_assessment', identity, self.source, data)
            tx.transfer(self.source, self.target)
            self.assertEqual(tx.get('cfde_assessment', identity)['data']['public']['status'], 'interrupted')

    def test_assessment_upload_references_outlive_deleted_editor_metadata(self):
        with self.repo.transaction() as tx:
            for identity in ('retained', 'unreferenced'):
                tx.put('upload', identity, self.source, {'id': identity, 'expires_at': '2000-01-01T00:00:00Z'})
            tx.put('cfde_assessment', uid(), self.source, {'composer': {'upload_ids': []},
                'inputs': {'uploads': [{'id': 'retained', 'sha256': '1' * 64,
                    'extraction': {'storage': {'sha256': '2' * 64, 'key': 'immutable-ref'}}}]}})
            self.assertEqual(user_inputs.referenced_ids(tx, self.source), {'retained'})
            user_inputs.cleanup(tx, self.source)
            self.assertIsNotNone(tx.get('upload', 'retained')); self.assertIsNone(tx.get('upload', 'unreferenced'))


if __name__ == '__main__': unittest.main()
