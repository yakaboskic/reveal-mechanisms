"""Captured authoring output reaches saving through deterministic gates only."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reveal_backend import acceptance
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import CAPTURE_MARKER, capture_binding, BoxTransportError
from reveal_backend.evidence_package import canonical_json, EvidenceBuildError, sha256
from reveal_backend.scientific_account_lint import AccountValidationError
from reveal_backend.workflow_execution import WorkflowExecution
import test_scientific_account_lint as account_fixtures


class WorkflowAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.science = account_fixtures.ScientificAccountLintTests
        cls.science.setUpClass()
        cls.addClassCleanup(cls.science.doClassCleanups)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = WorkflowExecution(repository=Mock())
        self.engine.activity = Mock()
        self.engine.checkpoint = Mock()
        self.payload = {'job_id': 'saved-authoring', 'namespace': 'test', 'generation': 2}
        self.queue = {'attempt': 1, 'inputs': {}, 'dispatch_input': {'model': 'original-author-model'}}
        self.execution = {'box': {'box_id': 'original-box'}, 'capture_complete': True,
                          'cleanup_complete': True, 'validated_paths': ['validated/stale.json']}
        (self.root / 'validated').mkdir()
        (self.root / 'validated/stale.json').write_text('{"stale":"must be ignored"}')
        self.context = ExitStack(); self.addCleanup(self.context.close)
        for name, value in (('release_root', lambda: self.science.release), ('LOCK', self.science.lock)):
            self.context.enter_context(patch.object(acceptance, name, value))
        self.context.enter_context(patch('reveal_backend.worker.LOCK', self.science.lock))
        self.context.enter_context(patch('reveal_backend.workflow_execution.read_preparation_inputs',
            return_value=({'attribution': {'user_id': 'owner', 'principal_kind': 'anonymous'}}, {})))
        self.context.enter_context(patch.dict('os.environ', {'ANTHROPIC_API_KEY': '',
            'REVEAL_GROUNDING_MAX_BUDGET_USD': 'invalid-unused-review-setting'}))
        self.reviewers = [self.context.enter_context(patch(name, side_effect=AssertionError('No second model may run')))
                          for name in ('reveal_backend.durable_review.call_one',
                                       'reveal_backend.scientific_grounding.review_account',
                                       'reveal_backend.scientific_grounding.review_paragraph',
                                       'reveal_backend.scientific_grounding.httpx.post')]

    def capture(self, kind='analysis', *, document=None, ledger_complete=True):
        self.job = {'id': self.payload['job_id'], 'kind': kind, 'owner_user_id': 'owner'}
        if kind == 'analysis':
            source = self.science.package_path
            self.inputs = self.science.package
            output = deepcopy(self.science.draft) if document is None else document
            filename = 'account-1.json'
        else:
            account = self.science.valid['scientific_accounts'][0]
            self.citation = {'target_id': account['component_claims'][0], 'citation_metadata_revision': 1}
            self.inputs = {'format': 'reveal.paragraph-input/1', 'account_document': self.science.valid,
                           'account_id': account['id'], 'allowed_citations': [self.citation]}
            source = self.root / 'paragraph-input.json'; source.write_bytes(canonical_json(self.inputs))
            output = {'format': 'reveal.paragraph-output/1', 'segments': [
                {'text': 'The captured observation supports a scoped assessment.', 'citations': [self.citation]}]} if document is None else document
            filename = 'paragraph.json'
        output_dir = self.root / 'attempt-1/output'
        (output_dir / 'output').mkdir(parents=True)
        (output_dir / 'ledger').mkdir()
        self.raw = output_dir / 'output' / filename; self.raw.write_bytes(canonical_json(output))
        self.request = ExecutionRequest(job_id=self.job['id'], attempt=1,
            kind='research' if kind == 'analysis' else 'paragraph', input_path=source, output_dir=output_dir)
        runtime = {'job_id': self.job['id'], 'attempt': 1, 'input_sha256': sha256(source.read_bytes()),
                   'model': self.queue['dispatch_input']['model'], 'dapper': json.loads(self.science.lock.read_bytes())}
        (output_dir / 'runtime.json').write_bytes(canonical_json(runtime))
        (output_dir / 'ledger/manifest.json').write_bytes(canonical_json({
            'format': 'reveal.tool-ledger/1', 'job_id': self.job['id'], 'attempt': 1,
            'complete': ledger_complete, 'calls': []}))
        names = ('output/' + filename, 'runtime.json', 'ledger/manifest.json')
        files = {name: {'sha256': sha256((output_dir / name).read_bytes()), 'size_bytes': (output_dir / name).stat().st_size}
                 for name in names}
        marker = {'format': 'reveal.box-capture/1', 'binding': capture_binding(self.request, self.execution['box']),
                  'state': {'status': 'succeeded'}, 'cleanup_complete': True, 'files': files}
        (output_dir / CAPTURE_MARKER).write_bytes(canonical_json(marker))

    async def validate(self):
        return await self.engine.validate(self.payload, 'validation-fence', self.job, self.queue,
                                          self.execution, self.root, self.request, self.inputs)

    def assert_no_reviewer(self):
        for reviewer in self.reviewers: reviewer.assert_not_called()
        self.assertFalse((self.root / 'review').exists())

    async def test_analysis_reassembles_stale_validated_paths_then_commits_without_review(self):
        self.capture()
        with patch('reveal_backend.workflow_execution.assemble_account', wraps=acceptance.assemble_account) as assemble:
            result = await self.validate()
        self.assertEqual(result, {'next_phase': 'commit'})
        assemble.assert_called_once()
        self.assertEqual(assemble.call_args.args[0], self.raw.resolve())
        self.assertEqual(self.engine.checkpoint.call_args.kwargs['validated_paths'], ['validated/account-0.json'])
        document = json.loads((self.root / 'validated/account-0.json').read_bytes())
        report = json.loads((self.root / 'validated/report-0.json').read_bytes())
        self.assertTrue(report['valid'], report)
        self.assertEqual(report['mode'], 'final')
        self.assertTrue(document['scientific_accounts'][0]['id'].startswith('dapper:ScientificAccount.'))
        self.assertEqual(report['document_sha256'], sha256((self.root / 'validated/account-0.json').read_bytes()))
        self.assert_no_reviewer()

    async def test_stale_validated_paths_cannot_bypass_invalid_ledger(self):
        self.capture(ledger_complete=False)
        with (patch('reveal_backend.workflow_execution.assemble_account', wraps=acceptance.assemble_account) as assemble,
              self.assertRaisesRegex(EvidenceBuildError, 'ledger is incomplete')):
            await self.validate()
        assemble.assert_not_called(); self.engine.checkpoint.assert_not_called()
        self.assert_no_reviewer()

    async def test_stale_validated_paths_cannot_bypass_final_lint(self):
        document = deepcopy(self.science.draft)
        document['scientific_accounts'][0]['closing_remarks'] = ''
        self.capture(document=document)
        with self.assertRaises(AccountValidationError) as failure:
            await self.validate()
        self.assertIn('account-synthesis', {finding['check'] for finding in failure.exception.report['findings']})
        self.engine.checkpoint.assert_not_called()
        diagnostic = json.loads((self.root / 'attempt-1/validation-1.json').read_bytes())
        self.assertFalse(diagnostic['valid'])
        self.assert_no_reviewer()

    async def test_altered_capture_is_rejected_before_reusing_stale_validation(self):
        self.capture()
        self.raw.write_bytes(b'{}')
        with self.assertRaises(BoxTransportError): await self.validate()
        self.engine.checkpoint.assert_not_called()
        self.assert_no_reviewer()

    async def test_valid_paragraph_reaches_commit_without_review(self):
        self.capture('paragraph')
        self.assertEqual(await self.validate(), {'next_phase': 'commit'})
        self.assertEqual(self.engine.checkpoint.call_args.kwargs['validated_paths'],
                         ['attempt-1/output/output/paragraph.json'])
        self.assert_no_reviewer()

    async def test_invalid_paragraph_segment_cannot_bypass_validation(self):
        self.capture('paragraph', document={'format': 'reveal.paragraph-output/1',
            'segments': [{'text': 'Uncited text', 'citations': []}]})
        with self.assertRaisesRegex(ValueError, 'does not cite an accepted account Claim'):
            await self.validate()
        self.engine.checkpoint.assert_not_called()
        self.assert_no_reviewer()
