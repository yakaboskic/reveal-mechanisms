"""File-backed worker and bundle boundaries, without network or model execution."""
import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import jobs, worker
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.box_adapter import make_bundle
from reveal_backend.dapper_release import prepare_agent_workspace
from reveal_backend.evidence_package import canonical_json, decode, sha256
from reveal_backend.repository import Repository, uid
from reveal_backend.runtime_config import ROOT
import test_evidence_package as fixtures


class WorkerFileEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = deepcopy(fixtures.EvidencePackageTests.built.package)
        gap = self.package['dapper_context']['knowledge_gaps'][0]
        factor = self.package['selection']['eaggl_mechanism_ids'][0]
        self.frozen = {'question_id': gap['id'], 'composer': {
            'source_gap': {'source_id': fixtures.GAP,
                'source_revision': self.package['dismech']['source_revision']['source_sha256']},
            'selected_kgs': self.package['external_evidence']['selected_graphs'],
            'dismissed_source_ids': [],
            'eaggl_anchors': [{'reference': {'source_id': factor}, 'origin': 'user_supplied'}]}}
        self.binding = {'source_gap': {'object': gap}, 'anchors': [{
            'cfde_node_id': factor, 'model': 'eaggl-capped-v1', 'gene_set_import_id': 'test-release', 'embedding_run_id': 'test-release'}]}

    def test_collection_uses_configured_source_bounds_independent_of_token_budget(self):
        calls = []

        def collect_fixture(**kwargs):
            calls.append(kwargs)
            fixtures.EvidencePackageTests.built.write(kwargs['output'] / 'package')
            return fixtures.EvidencePackageTests.built

        with patch.object(worker, 'collect_reference_package', side_effect=collect_fixture), \
                patch.object(worker, 'DapperRuntime', return_value=fixtures.EvidencePackageTests.runtime), \
                patch('httpx.post', side_effect=AssertionError('Collection must not request a model token count')):
            for index, token_budget in enumerate((1, 24000, 1000000)):
                path, package = worker.collect({}, self.frozen, self.binding,
                    {'evidence_tokens': token_budget, 'candidates_per_type': 37, 'max_nodes': 81, 'max_edges': 191},
                    self.root / ('capture-' + str(index)))
                self.assertEqual(package, self.package)
                self.assertEqual(path.read_bytes(), fixtures.EvidencePackageTests.built.files['evidence-package.json'])
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual((call['limit'], call['max_nodes'], call['max_edges']), (37, 81, 191))
            policy = call['selection_metadata']['collection_budget']
            self.assertEqual(policy['effective_candidates_per_type'], 37)
            self.assertNotIn('evidence_token_budget', policy)

    def test_complete_package_reaches_worker_adapter_and_bundle_without_counting(self):
        repository = Repository(str(self.root / 'app.sqlite')); repository.migrate()
        owner, request_id = uid(), uid()
        with repository.transaction() as tx:
            tx.put('request', request_id, owner, self.frozen)
            tx.put('request_binding', request_id, owner, self.binding)
            job = jobs.enqueue(tx, owner, 'analysis', request_id=request_id, inputs={'budgets': {'evidence_tokens': 1}})
        capture = self.root / job['id'] / 'evidence'
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', capture)
        package_path = capture / 'package/evidence-package.json'
        original = package_path.read_bytes()
        self.assertGreater(len(original), 24000)
        seen = []

        class OfflineAdapter:
            async def execute(inner, request, emit, cancelled, checkpoint):
                seen.append((request, make_bundle(ROOT, request)))
                return ExecutionResult('insufficient_evidence', request.output_dir,
                                       reason='Offline boundary check; no model was launched')

        with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': str(self.root)}), \
                patch('httpx.post', side_effect=AssertionError('No provider call is allowed in file preparation')):
            asyncio.run(worker.Worker(repository, OfflineAdapter()).process(*jobs.claim(repository, 'file-test')))
        self.assertEqual(len(seen), 1)
        request, bundle_bytes = seen[0]
        self.assertEqual(request.input_path, package_path)
        self.assertEqual(request.input_path.read_bytes(), original)
        with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode='r:gz') as bundle:
            self.assertEqual(bundle.extractfile('input/evidence-package.json').read(), original)
            manifest = json.load(bundle.extractfile('input/evidence-input.json'))
            self.assertEqual(manifest['format'], 'reveal.file-backed-evidence/2')
            self.assertEqual(manifest['package']['sha256'], sha256(original))
            self.assertIn('bundle/services/backend/src/reveal_backend/evidence_files.py', bundle.getnames())
            self.assertIn('bundle/services/backend/agent-skills/read-evidence-package/SKILL.md', bundle.getnames())
            self.assertNotIn('input/dispatch-view.json', bundle.getnames())
            for artifact in self.package['source_artifacts'].values():
                self.assertEqual(bundle.extractfile('input/' + artifact['path']).read(),
                                 (package_path.parent / artifact['path']).read_bytes())
        with repository.read_transaction() as tx:
            # This adapter deliberately stops after observing bundle bytes and
            # supplies no trusted outcome artifact. A reason alone is not saved.
            result=tx.get('job', job['id'])['data']
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failure']['code'], 'VALIDATION_FAILED')
            snapshot = tx.get('queue', job['id'])['data']['dispatch_input']
            self.assertEqual(snapshot['sha256'], sha256(original))
            self.assertEqual(tx.list('account', owner), [])
        prepared = decode((capture / 'dispatch-input.json').read_bytes())
        self.assertEqual(prepared['format'], 'reveal.dispatch-input/2')
        self.assertFalse(prepared['measurement']['enforced'])

    def test_workspace_preserves_json_bytes_and_installs_reader_with_resolved_links(self):
        capture = self.root / 'capture'
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', capture)
        package_path = capture / 'package/evidence-package.json'
        # A valid noncompact serialization must keep its exact input hash too.
        original = (json.dumps(self.package, ensure_ascii=False, indent=2) + '\n').encode()
        package_path.write_bytes(original)
        workspace = self.root / 'workspace'
        lock = ROOT / 'services/backend/agent-runtime/dapper-release.json'
        with patch('reveal_backend.dapper_release.clone_release', return_value={'commit': 'isolated-test-release'}) as clone:
            result = prepare_agent_workspace(workspace, ROOT, package_path, lock)
        clone.assert_called_once_with(workspace / 'dapper', lock.resolve())
        work = workspace / 'reveal'
        self.assertEqual(Path(result['evidence_package']).read_bytes(), original)
        self.assertEqual(result['evidence_package_sha256'], sha256(original))
        for name in ('construct-scientific-account', 'read-evidence-package'):
            relative = f'.claude/skills/{name}/SKILL.md'
            skill = work / relative
            self.assertTrue(skill.is_file())
            self.assertEqual(result['bundle_files'][relative], sha256(skill.read_bytes()))
            self.assertNotIn('../../../../docs/', skill.read_text())
        self.assertTrue((work / 'services/backend/src/reveal_backend/evidence_files.py').is_file())
        for artifact in self.package['source_artifacts'].values():
            self.assertEqual((work / 'input' / artifact['path']).read_bytes(),
                             (package_path.parent / artifact['path']).read_bytes())

    def test_collector_pins_exact_reader_skill_as_an_authoring_reference(self):
        relative = 'services/backend/agent-skills/read-evidence-package/SKILL.md'
        reference = next(row for row in self.package['authoring']['references'] if row['path'] == relative)
        original = (ROOT / relative).read_bytes()
        self.assertEqual(reference['sha256'], sha256(original))
        artifact = self.package['source_artifacts'][reference['artifact_id']]
        self.assertEqual(artifact['sha256'], sha256(original))
        self.assertEqual(fixtures.EvidencePackageTests.built.files[artifact['path']], original)


if __name__ == '__main__':
    unittest.main()
