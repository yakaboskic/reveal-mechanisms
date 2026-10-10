"""Real progressive seed, SQL receipt, validation and commit; only Box authorship is fake."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import acceptance, jobs, research_hosted
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json, decode, sha256
from reveal_backend.repository import Repository, uid
from reveal_backend.research_tools import dispatch
from reveal_backend.research_work import ResearchWorkService, authenticate, authorize_commit
from reveal_backend.worker import Worker
import test_scientific_account_lint as scientific_fixtures
import test_research_data as reference_fixtures
FACTOR = reference_fixtures.FACTOR


class HostedResearchJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.science = scientific_fixtures.ScientificAccountLintTests
        cls.science.setUpClass(); cls.addClassCleanup(cls.science.doClassCleanups)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root/'research.sqlite')); self.repo.migrate()
        self.owner = uid()
        self.reference = reference_fixtures.ReferenceQueryTests(); self.reference.setUp(); self.addCleanup(self.reference.doCleanups)
        self.service = ResearchWorkService(self.repo, data_service=self.reference.service)
        self.env = patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': str(self.root),
            'REVEAL_WORK_DIR': str(self.root), 'REVEAL_ARTIFACT_STORE': 'filesystem', 'REVEAL_PUBLIC_API_URL': 'http://127.0.0.1:18000',
            'REVEAL_QUEUE_NAMESPACE': 'hosted-fixture', 'REVEAL_CLAUDE_MODEL': 'fixture-model'})
        self.env.start(); self.addCleanup(self.env.stop)
        for target, value in [('reveal_backend.acceptance.release_root', lambda: self.science.release),
                              ('reveal_backend.acceptance.LOCK', self.science.lock), ('reveal_backend.worker.LOCK', self.science.lock)]:
            patcher = patch(target, value); patcher.start(); self.addCleanup(patcher.stop)
        for target in ('reveal_backend.worker.collect_reference_package',):
            patcher = patch(target, side_effect=AssertionError('Progressive startup cannot use an eager collector'))
            patcher.start(); self.addCleanup(patcher.stop)
        with self.repo.transaction() as tx:
            tx.put('principal', self.owner, self.owner, {'me': {'user_id': self.owner, 'principal_kind': 'anonymous', 'workspace_expires_at': None}})
        self.executions = []

    def new_job(self):
        _, frozen, binding = reference_fixtures.SeedTests().inputs()
        frozen['id'] = uid()
        frozen['composer']['source_gap']['id'] = frozen['question_id']
        frozen['attribution'] = {'user_id': self.owner, 'principal_kind': 'anonymous', 'display_name': None, 'orcid': None, 'orcid_authenticated': False}
        with self.repo.transaction() as tx:
            tx.put('request', frozen['id'], self.owner, frozen)
            tx.put('request_binding', frozen['id'], self.owner, binding)
            job = jobs.enqueue(tx, self.owner, 'analysis', request_id=frozen['id'], inputs={'budgets': {'max_accounts': 1}})
            research_hosted.create(tx, job, frozen, binding)
        return job

    def provider(self, *, reuse=False, corrupt_scope=False):
        test = self
        class CapturedBoxProvider:
            async def execute(self, request, emit, cancelled, checkpoint):
                test.executions.append(request)
                seed = decode(request.input_path.read_bytes())
                test.assertEqual(seed['retrieval_mode'], 'progressive')
                test.assertEqual(seed['authoring']['max_accounts'], 1, 'Frozen seed must tell the author its actual submission limit')
                test.assertTrue(seed['readiness']['seed_ready'])
                test.assertFalse(seed['readiness']['input_capture_complete'])
                test.assertEqual(seed['research_context']['research_request_id'], request.research_access['research_request_id'])
                authorization = 'Bearer '+request.research_access['token']
                before = len(test.reference.queries)
                receipt_ids, reuse_ids, account_paths = [], [], ()
                directory = request.output_dir; (directory/'output').mkdir(parents=True); (directory/'ledger').mkdir()
                if reuse:
                    candidates = dispatch(test.service, authorization, 'find_scientific_accounts',
                        {'research_request_id': seed['research_request_id'], 'filters': {'question': seed['selection']['knowledge_gap_id']}})
                    selected = {**candidates['items'][0], 'purpose': 'existing_account'}
                    receipt = dispatch(test.service, authorization, 'reuse_scientific_objects',
                        {'research_request_id': seed['research_request_id'], 'selections': [selected], 'idempotency_key': 'reuse-existing'})
                    reuse_ids = [receipt['id']]
                    outcome = {'format': 'reveal.research-outcome/1', 'status': 'succeeded', 'existing_account_ids': [selected['object_id']],
                               'receipt_ids': [], 'reuse_receipt_ids': reuse_ids}
                    (directory/'output/outcome.json').write_bytes(canonical_json(outcome))
                    test.assertEqual(len(test.reference.queries), before, 'Reusing science performs no fresh reference query')
                else:
                    test.assertEqual(before, 0, 'Seed preparation did not read a reference table')
                    operation = dispatch(test.service, authorization, 'get_factor_loadings', {'research_request_id': seed['research_request_id'],
                        'arguments': {'factor_id': FACTOR, 'kind': 'gene', 'limit': 1}, 'idempotency_key': 'one-relevant-query'})
                    test.service.run_operation(operation['operation_id'])
                    with test.repo.read_transaction() as tx:
                        completed = tx.get('research_operation', operation['operation_id'])['data']
                    test.assertEqual(completed['state'], 'succeeded', completed.get('error'))
                    receipt = completed['result']; receipt_ids = [receipt['receipt_id']]
                    source = receipt['dapper_context']['files'][0]
                    document = deepcopy(test.science.draft)
                    document['prefixes'] = seed['prefixes']
                    document['knowledge_gaps'] = seed['dapper_context']['knowledge_gaps']
                    document['mechanisms'] = seed['dapper_context']['mechanisms']
                    document['files'] = [source]
                    mechanism = document['mechanisms'][0]['id']
                    document['propositions'][0].update(subject_entity='urn:cfde:gene:GENE_A', object_entity=mechanism,
                        statement='A scoped interpretation of the captured GENE_A loading in this factor.')
                    document['evidence_items'][0].update(context='Captured loaded observation at `/result/items/0`.', was_derived_from=[source['id']])
                    document['scientific_accounts'][0]['question'] = seed['selection']['knowledge_gap_id']
                    document['used_edges'][0]['object'] = source['id']
                    path = directory/'output/account-1.json'; path.write_bytes(canonical_json(document)); account_paths = (path,)
                receipt_path = directory/'ledger/research-receipts.json'
                receipt_path.write_bytes(canonical_json({'format': 'reveal.hosted-research-receipts/1', **seed['research_context'],
                    'local_work_id': 'different-work' if corrupt_scope else request.job_id, 'receipt_ids': receipt_ids, 'reuse_receipt_ids': reuse_ids}))
                runtime = {'job_id': request.job_id, 'attempt': request.attempt, 'input_sha256': sha256(request.input_path.read_bytes()),
                           'model': 'fixture-model', 'dapper': decode(test.science.lock.read_bytes())}
                runtime_path = directory/'runtime.json'; runtime_path.write_bytes(canonical_json(runtime))
                ledger_path = directory/'ledger/manifest.json'; ledger_path.write_bytes(canonical_json({'format': 'reveal.tool-ledger/1',
                    'job_id': request.job_id, 'attempt': request.attempt, 'complete': True, 'calls': []}))
                return ExecutionResult('succeeded', directory, account_paths=account_paths, runtime_manifest_path=runtime_path,
                    ledger_manifest_path=ledger_path, research_receipts_path=receipt_path,
                    outcome_path=directory/'output/outcome.json' if reuse else None)
        return CapturedBoxProvider()

    def run_job(self, job, **options):
        worker = Worker(self.repo, self.provider(**options))
        asyncio.run(worker.process(*jobs.claim(self.repo, 'test-hosted-worker', job_id=job['id'])))
        with self.repo.read_transaction() as tx: result = tx.get('job', job['id'])['data']
        if not options.get('corrupt_scope'):
            self.assertEqual(result['status'], 'succeeded', [result, [p.read_text() for p in self.root.rglob('failure.json')]])
        return result

    def test_new_account_gets_default_paragraph_and_reuse_only_preserves_original_without_another_paragraph(self):
        first = self.run_job(self.new_job())
        self.assertEqual(len(first['result']['account_ids']), 1)
        self.assertEqual(len(first['result']['paragraph_job_ids']), 1)
        self.assertEqual(first['result']['reused_account_ids'], [])
        with self.repo.read_transaction() as tx:
            saved = tx.list('account', self.owner)[0]['data']
            before = deepcopy(saved['result']['document'])
            self.assertEqual(saved['result']['research_statement']['status'], 'queued')
            paragraphs = [r['id'] for r in tx.list('job', self.owner) if r['data']['kind'] == 'paragraph']
        second = self.run_job(self.new_job(), reuse=True)
        self.assertEqual(second['result']['account_ids'], [])
        self.assertEqual(second['result']['reused_account_ids'], first['result']['account_ids'])
        self.assertEqual(second['result']['paragraph_job_ids'], [])
        with self.repo.read_transaction() as tx:
            self.assertEqual([r['id'] for r in tx.list('job', self.owner) if r['data']['kind'] == 'paragraph'], paragraphs)
            self.assertEqual(tx.list('account', self.owner)[0]['data']['result']['document'], before)
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+self.executions[-1].research_access['token'])

    def test_wrong_work_receipt_capture_cannot_accept_or_launch_a_paragraph(self):
        result = self.run_job(self.new_job(), corrupt_scope=True)
        self.assertEqual(result['status'], 'failed')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('account', self.owner), [])
            self.assertEqual([r for r in tx.list('job', self.owner) if r['data']['kind'] == 'paragraph'], [])

    def test_hosted_grant_and_delegated_query_are_fenced_by_exact_attempt(self):
        job = self.new_job(); job, queue = jobs.claim(self.repo, 'grant-fence', job_id=job['id'])
        research_hosted.collect(self.repo, job, self.root/'seed')
        access = research_hosted.access(self.repo, job, queue['attempt'])
        authorization = 'Bearer '+access['token']
        operation = dispatch(self.service, authorization, 'get_factor_loadings', {'research_request_id': job['research_request_id'],
            'arguments': {'factor_id': FACTOR, 'kind': 'gene'}, 'idempotency_key': 'fenced-query'})
        with self.repo.transaction() as tx:
            value = tx.get('queue', job['id'])['data']; value['attempt'] += 1; tx.put('queue', job['id'], self.owner, value)
            with self.assertRaises(Problem): authenticate(tx, authorization)
            with self.assertRaises(Problem): authorize_commit(tx, tx.get('research_operation', operation['operation_id'])['data'])
        self.service.run_operation(operation['operation_id'])
        self.assertEqual(self.reference.queries, [], 'Stale delegated authority cannot start scientific reading')

    def test_hosted_account_limit_is_frozen_once_with_the_seed(self):
        job = self.new_job()
        path, package = research_hosted.collect(self.repo, job, self.root/'first')
        original = path.read_bytes()
        self.assertEqual(package['authoring']['max_accounts'], 1)
        with self.repo.transaction() as tx:
            queue = tx.get('queue', job['id'])['data']
            queue['inputs']['budgets']['max_accounts'] = 3
            tx.put('queue', job['id'], self.owner, queue)
        replay, package = research_hosted.collect(self.repo, job, self.root/'replay')
        self.assertEqual(replay.read_bytes(), original)
        self.assertEqual(package['authoring']['max_accounts'], 1)
