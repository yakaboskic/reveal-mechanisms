"""Source-artifact batching preserves the prior sequential put contract."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import jobs
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, sha256
from reveal_backend.repository import digest, uid
from reveal_backend.worker import Worker, persist_source_artifacts, prepare_source_artifacts
from test_worker_preparation import RecordingRepository


class WorkerArtifactBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = uid()

    def repository(self, name):
        repo = RecordingRepository(str(self.root / (name + '.sqlite')))
        repo.migrate()
        return repo

    def artifact(self, identity, value='original'):
        return {'sha256': sha256(str(identity).encode()), 'file': {'id': 'file:' + str(identity), 'name': value},
                'path': '/captured/' + str(identity), 'job_id': 'source-job'}

    def test_exact_rows_versions_and_ownership_match_sequential_puts(self):
        baseline, batched = self.repository('baseline'), self.repository('batched')
        old = self.artifact('existing')
        other_owner = uid()
        records = [self.artifact('new', 'first'), self.artifact('another'),
                   self.artifact('existing', 'second'), self.artifact('new', 'last'),
                   self.artifact('existing', 'third')]
        with patch('reveal_backend.repository.now', return_value='2026-09-28T00:00:00Z'):
            for repo in (baseline, batched):
                with repo.transaction() as tx:
                    tx.put('artifact', digest([self.owner, old['sha256']]), self.owner, old)
                    tx.put('artifact', digest([self.owner, old['sha256']]), self.owner, old)
                    tx.put('artifact', digest([other_owner, old['sha256']]), other_owner, old)
            with baseline.transaction() as tx:
                for record in records:
                    tx.put('artifact', digest([self.owner, record['sha256']]), self.owner, record)
            with batched.transaction() as tx:
                persist_source_artifacts(tx, self.owner, records)
        stored = []
        for repo in (baseline, batched):
            with repo.read_transaction() as tx:
                stored.append(tx.execute('SELECT id,owner_id,version,payload,updated_at FROM reveal_records WHERE kind=%s ORDER BY id', ('artifact',)).fetchall())
        self.assertEqual(stored[1], stored[0])
        with batched.read_transaction() as tx:
            self.assertEqual(tx.get('artifact', digest([self.owner, old['sha256']]))['version'], 4)
            self.assertEqual(tx.get('artifact', digest([other_owner, old['sha256']]))['version'], 1)

    def test_new_artifacts_use_bounded_select_and_insert_batches(self):
        repo = self.repository('counts')
        records = [self.artifact(index) for index in range(250)]
        repo.operations.clear()
        with repo.transaction() as tx:
            persist_source_artifacts(tx, self.owner, records)
        statements = repo.operations[0][1]
        self.assertEqual([sql.split()[0] for sql, _ in statements], ['SELECT'] * 3 + ['INSERT'] * 3)
        self.assertEqual([len(params) - 1 for sql, params in statements if sql.startswith('SELECT')], [100, 100, 50])
        self.assertEqual([len(params) // 6 for sql, params in statements if sql.startswith('INSERT')], [100, 100, 50])
        with repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('artifact', self.owner)), 250)
        # Original per-record put used 500 application statements for these rows.

    def test_late_insert_failure_rolls_back_earlier_updates_and_batches(self):
        repo = self.repository('rollback')
        old = self.artifact('old')
        with repo.transaction() as tx:
            tx.put('artifact', digest([self.owner, old['sha256']]), self.owner, old)
        with self.assertRaisesRegex(OSError, 'second batch'):
            with repo.transaction() as tx:
                original = tx.insert_many
                calls = []

                def fail_second(records):
                    calls.append(True)
                    if len(calls) == 2:
                        raise OSError('Injected second batch failure')
                    return original(records)

                tx.insert_many = fail_second
                persist_source_artifacts(tx, self.owner,
                    [self.artifact('old', 'changed')] + [self.artifact(index) for index in range(101)])
        with repo.read_transaction() as tx:
            rows = tx.list('artifact', self.owner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data'], old)
        self.assertEqual(rows[0]['version'], 1)

    def package(self):
        data = b'Exact source bytes\n'
        source = self.root / 'source.json'
        source.write_bytes(data)
        file = {'id': 'dapper:File.exact', 'sha256': sha256(data)}
        package = {'dapper_context': {'files': [file]}, 'source_artifacts': {
            'captured': {'dapper_file_id': file['id'], 'path': source.name, 'sha256': sha256(data)}}}
        path = self.root / 'package.json'
        path.write_bytes(canonical_json(package))
        return path, package, source

    def test_source_bytes_and_paths_are_checked_before_any_acceptance_transaction(self):
        path, package, source = self.package()
        records, access = prepare_source_artifacts(path, 'job')
        self.assertEqual(records[0]['path'], str(source.resolve()))
        self.assertEqual(records[0]['file'], package['dapper_context']['files'][0])
        self.assertEqual(access[records[0]['file']['id']]['verification'], 'checksum_verified')
        source.write_bytes(b'Changed bytes')
        repo = self.repository('changed')
        with patch.object(repo, 'transaction', side_effect=AssertionError('Must validate files before locking')):
            with self.assertRaisesRegex(EvidenceBuildError, 'Source artifact changed'):
                asyncio.run(Worker(repo).accept_accounts({'id': 'job'}, 'token', [], {}, path, None, self.root, 'deterministic'))
        package['source_artifacts']['captured']['path'] = '../outside.json'
        path.write_bytes(canonical_json(package))
        with self.assertRaisesRegex(EvidenceBuildError, 'escapes attempt directory'):
            prepare_source_artifacts(path, 'job')

    def test_stale_or_cancelled_acceptance_cannot_persist_prepared_artifacts(self):
        path, _, _ = self.package()
        repo = self.repository('fenced')
        with repo.transaction() as tx:
            jobs.enqueue(tx, self.owner, 'analysis', request_id=uid())
        job, queue = jobs.claim(repo, 'test')
        worker = Worker(repo)
        asyncio.run(worker.accept_accounts(job, 'stale', [], {}, path, None, self.root, 'deterministic'))
        with repo.transaction() as tx:
            jobs.cancel(tx, tx.get('job', job['id'])['data'])
        asyncio.run(worker.accept_accounts(job, queue['token'], [], {}, path, None, self.root, 'deterministic'))
        with repo.read_transaction() as tx:
            self.assertEqual(tx.list('artifact'), [])
            self.assertEqual(tx.list('account'), [])
            self.assertEqual(tx.list('outbox'), [])
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'cancel_requested')


if __name__ == '__main__':
    unittest.main()
