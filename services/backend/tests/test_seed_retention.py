"""Seed persistence is bounded, atomic and independent of unrelated work."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, Transaction, digest
from reveal_backend.research_work import ResearchWorkService, work_records
from reveal_backend import user_inputs


class SeedRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Repository(str(Path(self.directory.name)/'app.sqlite'))
        self.repo.migrate()
        self.service = ResearchWorkService(self.repo)
        self.env = patch.dict(os.environ, {'REVEAL_ARTIFACT_STORE': 'filesystem',
            'REVEAL_ARTIFACTS_DIR': self.directory.name, 'REVEAL_NOTIFICATION_REDIS_URL': '',
            'REVEAL_NOTIFICATION_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_URL': '',
            'UPSTASH_REDIS_REST_TOKEN': ''})
        self.env.start(); self.addCleanup(self.env.stop)
        with self.repo.transaction() as tx:
            tx.put('local_work', 'work', 'owner', {'id': 'work', 'job_id': 'fixture-hosted'})
        self.files = {f'sources/{i}.json': json.dumps({'index': i, 'exact': '0.12345678901234567890'}).encode()
            for i in range(41)}

    def rows(self):
        with self.repo.read_transaction() as tx:
            return work_records(tx, 'research_artifact', 'owner', 'work')

    def test_complete_seed_uses_one_batch_and_preserves_exact_originals_and_identities(self):
        statements = []
        execute = Transaction.execute
        def counted(tx, sql, params=()):
            statements.append(sql)
            return execute(tx, sql, params)
        with patch.object(Transaction, 'execute', counted):
            result = self.service.retain_seed('owner', 'work', self.files)
        self.assertEqual(len(result), 41)
        self.assertEqual(len(statements), 7)
        self.assertEqual(sum(sql.startswith('INSERT') for sql in statements), 1)
        for filename, value in zip(self.files, result):
            raw = self.files[filename]
            checksum = hashlib.sha256(raw).hexdigest()
            self.assertEqual(value['id'], digest(['work', 'seed', checksum, filename, {}]))
            self.assertEqual(value['sha256'], checksum)
            self.assertEqual(value['size_bytes'], len(raw))
            self.assertEqual(user_inputs.read(value['storage']), raw)

    def test_duplicate_bytes_share_one_upload_and_retry_does_not_retain_again(self):
        files = {'sources/a.json': b'{"exact":1.234567890123456789}',
            'package-sections/a.json': b'{"exact":1.234567890123456789}'}
        with patch.object(user_inputs, 'retain', wraps=user_inputs.retain) as retain:
            first = self.service.retain_seed('owner', 'work', files)
            self.assertEqual(retain.call_count, 1)
        with patch.object(user_inputs, 'retain', side_effect=AssertionError('Retry uploaded')):
            self.assertEqual(self.service.retain_seed('owner', 'work', files), first)
        self.assertNotEqual(first[0]['id'], first[1]['id'])
        self.assertEqual(first[0]['storage'], first[1]['storage'])

    def test_restart_reuses_partial_historical_seed_retention(self):
        filename, data = next(iter(self.files.items()))
        first = self.service.retain('owner', 'work', data, filename, purpose='seed')
        with patch.object(user_inputs, 'retain', wraps=user_inputs.retain) as retain:
            result = self.service.retain_seed('owner', 'work', self.files)
        self.assertEqual(result[0], first)
        self.assertEqual(retain.call_count, 40)

    def test_failed_blob_transfer_never_commits_incomplete_seed(self):
        retain = user_inputs.retain
        def failing(raw, media):
            if raw == self.files['sources/3.json']: raise RuntimeError('Storage interrupted')
            return retain(raw, media)
        with patch.object(user_inputs, 'retain', failing), self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.service.retain_seed('owner', 'work', self.files)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.service.retain_seed('owner', 'work', self.files)), 41)

    def test_blob_transfer_runs_without_write_lock_and_rechecks_owner(self):
        original = user_inputs.retain
        def store_and_transfer(raw, media):
            with self.repo.transaction() as tx:
                tx.put('local_work', 'work', 'other', {'id': 'work', 'job_id': 'fixture-hosted'})
            return original(raw, media)
        with patch.object(user_inputs, 'retain', store_and_transfer), self.assertRaises(Problem) as caught:
            self.service.retain_seed('owner', 'work', {'one.json': b'{}'})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.rows(), [])

    def test_quota_race_during_upload_is_rechecked_before_batch_commit(self):
        original = user_inputs.retain
        def consume_quota(raw, media):
            with self.repo.transaction() as tx:
                tx.put('research_artifact', 'concurrent', 'owner', {'id': 'concurrent', 'local_work_id': 'work',
                    'sha256': 'a'*64, 'size_bytes': 128_000_000, 'retained_record': True})
            return original(raw, media)
        with patch.object(user_inputs, 'retain', consume_quota), self.assertRaises(Problem) as caught:
            self.service.retain_seed('owner', 'work', {'one.json': b'{}'})
        self.assertEqual(caught.exception.code, 'RESEARCH_STORAGE_LIMIT')
        self.assertEqual([row['id'] for row in self.rows()], ['concurrent'])

    def test_concurrent_identical_seeds_commit_one_inventory(self):
        barrier = threading.Barrier(2)
        original = user_inputs.retain
        def simultaneous(raw, media):
            barrier.wait(timeout=5)
            return original(raw, media)
        # Separate instances model an operation retried by another replica.
        with patch.object(user_inputs, 'retain', simultaneous), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ResearchWorkService(self.repo).retain_seed,
                'owner', 'work', {'one.json': b'{}'}) for _ in range(2)]
            first, second = [future.result(timeout=10) for future in futures]
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)

    def test_work_scoping_does_not_decode_unrelated_or_other_owner_records(self):
        with self.repo.transaction() as tx:
            for identity, owner, work in [('mine', 'owner', 'work'), ('different-work', 'owner', 'other'),
                    ('different-owner', 'other', 'work')]:
                tx.put('research_operation', identity, owner, {'local_work_id': work, 'kind': 'query'})
        with self.repo.read_transaction() as tx, patch('reveal_backend.research_work.json.loads', wraps=json.loads) as decode:
            rows = work_records(tx, 'research_operation', 'owner', 'work')
        self.assertEqual([row['id'] for row in rows], ['mine'])
        self.assertEqual(decode.call_count, 1)


if __name__ == '__main__': unittest.main()
