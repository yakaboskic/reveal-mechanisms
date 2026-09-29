"""An explicit review retry must never relaunch authoring or trust changed output."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from reveal_backend import jobs
from reveal_backend.box_adapter import CAPTURE_MARKER
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.job_failures import authoring_failure, review_failure
from reveal_backend.repository import uid, digest
from reveal_backend.scientific_grounding import ScientificReviewUnavailable, _ReviewSession, MODEL
from reveal_backend.worker import Worker, LOCK
from test_application import ApplicationTests


class ReviewRetryTests(unittest.TestCase):
    setUp = ApplicationTests.setUp
    provision = ApplicationTests.provision
    token = ApplicationTests.token
    headers = ApplicationTests.headers

    def saved_job(self, kind='analysis'):
        owner, request_id = self.provision(), uid()
        frozen = {'composer': {'selected_kgs': []}, 'attribution': {'user_id': owner}}
        with self.repo.transaction() as tx:
            tx.put('request', request_id, owner, frozen)
            tx.put('request_binding', request_id, owner, {})
            if kind == 'analysis': jobs.enqueue(tx, owner, kind, request_id=request_id)
            else:
                account_id = 'dapper:ScientificAccount.' + 'a' * 32
                tx.put('account', digest([owner, account_id]), owner, {'result': {'document': {}, 'citation_metadata': []}, 'summary': {}})
                jobs.enqueue(tx, owner, kind, account_id=account_id)
        job, queue = jobs.claim(self.repo, 'source-author')
        root = Path(self.temp.name) / job['id']
        source = root / 'evidence/package.json'; source.parent.mkdir(parents=True); source.write_text('{}')
        output = root / 'attempt-1/output'; (output / 'output').mkdir(parents=True); (output / 'ledger').mkdir()
        authored = 'output/account-1.json' if kind == 'analysis' else 'output/paragraph.json'
        (output / authored).write_text('{}')
        runtime = {'job_id': job['id'], 'attempt': 1, 'model': 'original-author-model',
                   'input_sha256': sha256(source.read_bytes()), 'dapper': json.loads(LOCK.read_text())}
        (output / 'runtime.json').write_bytes(canonical_json(runtime))
        (output / 'ledger/manifest.json').write_bytes(canonical_json({
            'format': 'reveal.tool-ledger/1', 'job_id': job['id'], 'attempt': 1, 'complete': True, 'calls': []}))
        files = {name: {'sha256': sha256((output / name).read_bytes()), 'size_bytes': (output / name).stat().st_size}
                 for name in (authored, 'runtime.json', 'ledger/manifest.json')}
        marker = {'format': 'reveal.box-capture/1', 'cleanup_complete': True, 'state': {'status': 'succeeded'},
                  'binding': {'job_id': job['id'], 'attempt': 1, 'kind': 'research' if kind == 'analysis' else 'paragraph', 'box_id': 'original-box',
                              'selected_graphs': [], 'input_sha256': runtime['input_sha256']}, 'files': files}
        (output / CAPTURE_MARKER).write_bytes(canonical_json(marker))
        with self.repo.transaction() as tx:
            queue.update(dispatch_input={'path': 'evidence/package.json', 'sha256': runtime['input_sha256'],
                         'mode': 'box', 'model': runtime['model'], 'kind': kind},
                         remote_handle={'box_id': 'original-box', 'phase': 'deleted'})
            tx.put('queue', job['id'], owner, queue)
        jobs.finish(self.repo, job['id'], queue['token'], 'failed', failure={
            'code': 'REVIEW_UNAVAILABLE', 'message': 'No review verdict', 'retryable': True})
        (root / 'attempt-1/failure.json').write_text('{"original_failure":true}')
        with self.repo.read_transaction() as tx:
            job = tx.get('job', job['id'])['data']
        env = patch.dict('os.environ', {'REVEAL_ARTIFACTS_DIR': self.temp.name,
            'REVEAL_CLAUDE_MODEL': 'new-author-model', 'REVEAL_GROUNDING_MAX_BUDGET_USD': '10'})
        env.start(); self.addCleanup(env.stop)
        return owner, job, root

    def retry(self, owner, job, headers=None):
        return self.client.post('/v1/jobs/' + job['id'] + '/retry-review',
            json={'expected_last_event_id': job['last_event_id']}, headers=headers or self.headers(owner))

    def test_owner_idempotency_and_stale_state_do_not_duplicate_review(self):
        owner, job, root = self.saved_job(); headers = self.headers(owner)
        with self.repo.read_transaction() as tx: old_token = tx.get('queue', job['id'])['data']['token']
        self.assertEqual(self.retry(self.provision(), job).status_code, 404)
        url = '/v1/jobs/' + job['id'] + '/retry-review'
        self.assertEqual(self.client.post(url, json={'expected_last_event_id': job['last_event_id']}).status_code, 401)
        self.assertEqual(self.retry(owner, dict(job, last_event_id='0')).status_code, 409)
        first = self.retry(owner, job, headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json(), self.retry(owner, job, headers).json())
        self.assertEqual(self.retry(owner, job).status_code, 409)
        self.assertFalse(jobs.finish(self.repo, job['id'], old_token, 'succeeded'))
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('job', owner)), 1)
            queue = tx.get('queue', job['id'])['data']
            self.assertEqual(queue['review_source']['attempt'], 1)
            self.assertIsNone(queue['remote_handle']); self.assertIsNone(queue['token'])

    def test_scientific_rejection_and_changed_capture_cannot_be_retried(self):
        owner, job, root = self.saved_job()
        with self.repo.transaction() as tx:
            changed = deepcopy(job); changed['failure']['code'] = 'VALIDATION_FAILED'
            tx.put('job', job['id'], owner, changed)
        self.assertEqual(self.retry(owner, job).json()['code'], 'REVIEW_RETRY_UNAVAILABLE')
        with self.repo.transaction() as tx: tx.put('job', job['id'], owner, job)
        (root / 'attempt-1/output/output/account-1.json').write_text('{"changed":true}')
        self.assertEqual(self.retry(owner, job).json()['code'], 'REVIEW_CAPTURE_UNAVAILABLE')
        with self.repo.read_transaction() as tx: self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')

    def exercise_worker(self, verdict, tamper=False):
        owner, job, root = self.saved_job()
        response = self.retry(owner, job); self.assertEqual(response.status_code, 202, response.text)
        if tamper: (root / 'attempt-1/output/output/account-1.json').write_text('{"changed":true}')
        adapter = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError('Authoring must not execute')))
        worker = Worker(self.repo, adapter); worker.accept_accounts = AsyncMock()
        with patch('reveal_backend.worker.collect', side_effect=AssertionError('Must not recollect')) as collect, \
                patch('reveal_backend.worker.assemble_account', return_value=({'claims': []}, {'valid': True})) as assemble, \
                patch('reveal_backend.scientific_grounding.review_account', return_value={'accepted': verdict}) as review:
            asyncio.run(worker.process(*jobs.claim(self.repo, 'review-only-worker')))
        adapter.execute.assert_not_awaited(); collect.assert_not_called()
        if tamper: review.assert_not_called(); worker.accept_accounts.assert_not_awaited()
        else:
            self.assertEqual(assemble.call_args.args[5], 1, 'Retain original authoring attempt in provenance')
            self.assertEqual(review.call_args.kwargs['model'], MODEL)
            self.assertEqual(review.call_args.kwargs['max_budget_usd'], 10)
            self.assertTrue((root / 'attempt-2/grounding-1.json').exists())
            if verdict: worker.accept_accounts.assert_awaited_once()
            else:
                worker.accept_accounts.assert_not_awaited()
                with self.repo.read_transaction() as tx:
                    self.assertEqual(tx.get('job', job['id'])['data']['failure']['code'], 'VALIDATION_FAILED')
        self.assertEqual((root / 'attempt-1/failure.json').read_text(), '{"original_failure":true}')

    def test_review_success_can_persist_without_running_agent(self): self.exercise_worker(True)
    def test_negative_review_does_not_accept(self): self.exercise_worker(False)
    def test_tampering_after_queueing_is_checked_again(self): self.exercise_worker(True, tamper=True)

    def test_s3_review_retry_restores_empty_worker_without_relaunching_author(self):
        import shutil
        from reveal_backend.artifact_store import S3Store
        from test_deployment import MemoryS3
        owner, job, root = self.saved_job()
        storage = S3Store('reveal-test-artifacts', client=MemoryS3())
        marker = root / 'attempt-1/output' / CAPTURE_MARKER
        source = {'attempt': 1, 'box_id': 'original-box', 'capture_sha256': sha256(marker.read_bytes())}
        reference = storage.snapshot(root)
        with self.repo.transaction() as tx:
            queue = tx.get('queue', job['id'])['data']
            queue.update(workspace=reference, review_capture=source)
            tx.put('queue', job['id'], owner, queue)
        shutil.rmtree(root)
        adapter = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError('No second authoring run')))
        worker = Worker(self.repo, adapter); worker.accept_accounts = AsyncMock()
        with patch.dict('os.environ', {'REVEAL_ARTIFACT_STORE': 's3', 'REVEAL_WORK_DIR': self.temp.name}), \
                patch('reveal_backend.worker.artifact_store', return_value=storage), \
                patch('reveal_backend.worker.collect', side_effect=AssertionError('No recollection')), \
                patch('reveal_backend.worker.assemble_account', return_value=({'claims': []}, {'valid': True})), \
                patch('reveal_backend.scientific_grounding.review_account', return_value={'accepted': True}) as review:
            self.assertEqual(self.retry(owner, job).status_code, 202)
            asyncio.run(worker.process(*jobs.claim(self.repo, 'fresh-worker')))
        adapter.execute.assert_not_awaited(); worker.accept_accounts.assert_awaited_once(); review.assert_called_once()
        self.assertFalse(root.exists(), 'Worker scratch must be removed after durable capture')
        with self.repo.read_transaction() as tx: saved = tx.get('queue', job['id'])['data']['workspace']
        restored = Path(self.temp.name) / 'audit'
        storage.restore(saved, restored)
        self.assertEqual((restored / 'attempt-1/failure.json').read_text(), '{"original_failure":true}')

    def test_paragraph_review_also_reuses_its_own_saved_output(self):
        owner, job, root = self.saved_job('paragraph')
        self.assertEqual(self.retry(owner, job).status_code, 202)
        adapter = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError('No authoring')))
        worker = Worker(self.repo, adapter); worker.accept_paragraph = AsyncMock()
        with patch('reveal_backend.scientific_grounding.review_paragraph', return_value={'accepted': True}) as review:
            asyncio.run(worker.process(*jobs.claim(self.repo, 'paragraph-review')))
        adapter.execute.assert_not_awaited(); worker.accept_paragraph.assert_awaited_once()
        self.assertEqual(review.call_args.kwargs['max_budget_usd'], 10)
        self.assertTrue((root / 'attempt-2/paragraph-grounding.json').exists())

    def test_second_review_retry_keeps_the_original_capture(self):
        owner, job, root = self.saved_job()
        self.assertEqual(self.retry(owner, job).status_code, 202)
        worker = Worker(self.repo)
        with patch('reveal_backend.worker.assemble_account', return_value=({'claims': []}, {'valid': True})), \
                patch('reveal_backend.scientific_grounding.review_account', side_effect=ScientificReviewUnavailable('Service unavailable')):
            asyncio.run(worker.process(*jobs.claim(self.repo, 'review-unavailable')))
        with self.repo.read_transaction() as tx: again = tx.get('job', job['id'])['data']
        self.assertEqual(self.retry(owner, again).status_code, 202)
        with self.repo.read_transaction() as tx: source = tx.get('queue', job['id'])['data']['review_source']
        self.assertEqual(source['attempt'], 1)
        self.assertTrue((root / 'attempt-2/failure.json').exists())


class BudgetFailureTests(unittest.TestCase):
    def test_context_and_turn_limits_are_configurable_and_reported(self):
        payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'x' * 100_000}], 'max_tokens': 3072}
        from unittest.mock import Mock
        client = Mock()
        body = {'usage': {'input_tokens': 100_000, 'output_tokens': 2}}
        client.post.return_value = SimpleNamespace(status_code=200, json=lambda: body)
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_REQUEST_BYTES': '98304'}):
            small = _ReviewSession(MODEL, 'offline', 10, client)
            with self.assertRaises(ScientificReviewUnavailable): small.post(payload)
        client.post.assert_not_called()
        failure = review_failure(ScientificReviewUnavailable('Limit', small.audit()))
        self.assertIn('context size in bytes', failure['message'])
        self.assertIn('98,304', failure['message'])
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_REQUEST_BYTES': '524288',
                'REVEAL_GROUNDING_MAX_TURNS': '32', 'REVEAL_GROUNDING_MAX_INPUT_TOKENS': '128000'}):
            large = _ReviewSession(MODEL, 'offline', 10, client)
            large.calls = [{}] * 8
            large.post(payload)
            self.assertEqual(len(large.calls), 9)
            self.assertEqual(large.audit()['configured_limits']['input_tokens'], 128000)
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_TURNS': '1'}):
            limited = _ReviewSession(MODEL, 'offline', 10, client)
            limited.calls = [{}]
            with self.assertRaises(ScientificReviewUnavailable): limited.post(payload)
            self.assertIn('model turns', review_failure(ScientificReviewUnavailable('Limit', limited.audit()))['message'])

    def test_invalid_review_limit_settings_fail_before_calling_provider(self):
        for value in ('0', '-1', '1.5', 'NaN', 'invalid'):
            with self.subTest(value=value), patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_TURNS': value}):
                with self.assertRaises(ScientificReviewUnavailable): _ReviewSession(MODEL, 'offline', 10, None)

    def test_review_budget_explains_cap_actual_spend_and_reservation(self):
        session = _ReviewSession(MODEL, 'offline', .001, None)
        with self.assertRaises(ScientificReviewUnavailable):
            session.post({'model': MODEL, 'messages': [], 'max_tokens': 3072})
        failure = review_failure(ScientificReviewUnavailable('Scientific review remaining budget is insufficient', session.audit()))
        self.assertEqual(failure['code'], 'REVIEW_BUDGET_EXCEEDED')
        self.assertEqual(failure['budget']['spent_usd'], 0)
        self.assertGreater(failure['budget']['next_call_max_usd'], .001)
        self.assertIn('No scientific verdict', failure['message'])

    def test_large_configured_caps_work_but_nonfinite_caps_do_not(self):
        self.assertEqual(_ReviewSession(MODEL, 'offline', 10, None).budget, 10)
        for cap in (0, -1, float('inf'), float('nan'), '10', True):
            with self.subTest(cap=cap), self.assertRaises(ScientificReviewUnavailable):
                _ReviewSession(MODEL, 'offline', cap, None)

    def test_legacy_budget_audit_and_generic_unavailability_stay_distinct(self):
        failure = review_failure(ScientificReviewUnavailable('Scientific review remaining budget is insufficient', {
            'configured_max_usd': .3, 'actual_cost_usd': .207882}))
        self.assertEqual(failure['code'], 'REVIEW_BUDGET_EXCEEDED')
        self.assertIn('$0.30', failure['message']); self.assertIn('$0.2079', failure['message'])
        self.assertEqual(review_failure(ScientificReviewUnavailable('HTTP failure'))['code'], 'REVIEW_UNAVAILABLE')

    def test_authoring_budget_reports_missing_usage_honestly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runtime.json'
            for cost in (None, 2.9):
                path.write_text(json.dumps({'completion': {'provider_result_subtype': 'error_max_budget_usd', 'cost_usd': cost, 'max_budget_usd': 3}}))
                failure = authoring_failure(SimpleNamespace(runtime_manifest_path=path), SimpleNamespace(max_budget_usd=25))
                self.assertEqual(failure['budget']['limit_usd'], 3)
                self.assertEqual(failure['budget']['spent_usd'], cost)
                self.assertEqual(failure['code'], 'AUTHORING_BUDGET_EXCEEDED')
