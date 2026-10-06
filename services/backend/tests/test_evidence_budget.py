"""File-backed preparation never treats a stored package as inline model context."""
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.evidence_budget import fit_input_budget
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, sha256
from reveal_backend.dispatch_view import (FILE_INPUT_FILENAME, BUDGET_FILENAME, VIEW_FILENAME, VIEW_FORMAT,
    BUDGET_SCOPE, dispatch_view, legacy_research_prompt, measured_input, research_prompt, validate_file_input)
from reveal_backend.runtime_config import ROOT, setting
import test_evidence_package as fixtures


class BudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'capture').resolve()
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', self.root)
        self.path = self.root / 'package/evidence-package.json'

    def test_complete_package_freezes_without_count_service_or_candidate_reduction(self):
        originals = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        original = json.loads(self.path.read_bytes())
        # A token threshold smaller than the file demonstrates that this is an
        # artifact, not an inline model request. No API key or network is needed.
        with patch('httpx.post', side_effect=AssertionError('No token-count export')), \
                patch('socket.create_connection', side_effect=AssertionError('No network')):
            chosen, package, prepared = fit_input_budget(self.path, 'box', 1)
            self.assertEqual(fit_input_budget(self.path, 'box', 1), (chosen, package, prepared))
        self.assertEqual(chosen, self.path)
        self.assertEqual(package, original)
        self.assertEqual(prepared['method'], 'file-backed-evidence')
        self.assertFalse(prepared['enforced']); self.assertFalse(prepared['reduced'])
        self.assertNotIn('count', prepared)
        self.assertEqual(prepared['package_bytes'], len(originals['package/evidence-package.json']))
        for name, raw in originals.items(): self.assertEqual((self.root / name).read_bytes(), raw, name)
        self.assertFalse((self.root / 'dispatch/package').exists())
        self.assertFalse((self.root / VIEW_FILENAME).exists())
        metadata = json.loads((self.root / FILE_INPUT_FILENAME).read_bytes())
        prompt = research_prompt(package['external_evidence']['selected_graphs'])
        validate_file_input(chosen.read_bytes(), metadata, prompt)
        self.assertLess(len(prompt.encode()), 12000)
        self.assertNotIn(chosen.read_text(), prompt)

    def test_frozen_package_request_and_manifest_are_immutable(self):
        fit_input_budget(self.path, 'box', 24000)
        for mode, budget in [('deterministic', 24000), ('box', 30000)]:
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, mode, budget)
        metadata = self.root / FILE_INPUT_FILENAME
        original = metadata.read_bytes(); metadata.write_text('{}')
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)
        metadata.write_bytes(original)
        self.path.write_text('{}')
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)

    def test_feedback_is_bound_without_sending_evidence_for_token_measurement(self):
        feedback = ('Only assert directions supported by exact source evidence.',)
        chosen, package, prepared = fit_input_budget(self.path, 'box', 24000, feedback)
        self.assertEqual(fit_input_budget(self.path, 'box', 24000, feedback), (chosen, package, prepared))
        for changed in [(), ('Different feedback.',)]:
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000, changed)
        metadata = json.loads((self.root / FILE_INPUT_FILENAME).read_bytes())
        with self.assertRaises(EvidenceBuildError):
            validate_file_input(chosen.read_bytes(), metadata, research_prompt(package['external_evidence']['selected_graphs']))

    def test_bundle_preserves_sources_and_requires_frozen_file_manifest_on_resume(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import BoxConfigurationError, make_bundle
        chosen, package, _ = fit_input_budget(self.path, 'box', 24000)
        request = ExecutionRequest('test-job', 1, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT, request)), mode='r:gz') as archive:
            self.assertEqual(archive.extractfile('input/evidence-package.json').read(), chosen.read_bytes())
            self.assertEqual(archive.extractfile('input/' + FILE_INPUT_FILENAME).read(),
                             (self.root / FILE_INPUT_FILENAME).read_bytes())
            self.assertIn('bundle/services/backend/agent-skills/read-evidence-package/SKILL.md', archive.getnames())
            self.assertIn('bundle/services/backend/src/reveal_backend/evidence_files.py', archive.getnames())
            for module in ('authoring_structure.py','box_timing.py'):
                self.assertEqual(archive.extractfile('bundle/services/backend/src/reveal_backend/'+module).read(),
                                 (ROOT/'services/backend/src/reveal_backend'/module).read_bytes())
            for item in package['source_artifacts'].values():
                self.assertEqual(archive.extractfile('input/' + item['path']).read(),
                                 (chosen.parent / item['path']).read_bytes())
        (self.root / FILE_INPUT_FILENAME).unlink()
        with self.assertRaises(BoxConfigurationError): make_bundle(ROOT, request)

    def test_legacy_capture_keeps_exact_bytes_and_historical_measurement_scope(self):
        original = self.path.read_bytes()
        saved = {'format': 'reveal.dispatch-input/1', 'binding': {'original_sha256': sha256(original),
                 'mode': 'box', 'model': setting('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6'), 'budget': 24000},
                 'path': 'package/evidence-package.json', 'sha256': sha256(original),
                 'measurement': {'enforced': True, 'count': 12000, 'budget': 24000}}
        manifest = canonical_json(saved); (self.root / 'dispatch-input.json').write_bytes(manifest)
        with patch('httpx.post', side_effect=AssertionError('No remeasurement')):
            chosen, _, measured = fit_input_budget(self.path, 'box', 24000)
        self.assertEqual(chosen.read_bytes(), original)
        self.assertIn('legacy', measured['scope'])
        self.assertFalse((self.root / FILE_INPUT_FILENAME).exists())
        self.assertEqual((self.root / 'dispatch-input.json').read_bytes(), manifest)

    def test_legacy_compact_manifest_validates_original_prompt_not_new_reader(self):
        original = self.path.read_bytes(); package = json.loads(original)
        model = setting('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6')
        view = dispatch_view(original); prompt = legacy_research_prompt(package['external_evidence']['selected_graphs'])
        measurement = {'method': 'anthropic-count-tokens', 'scope': BUDGET_SCOPE, 'enforced': True,
                       'count': 12000, 'budget': 24000, 'model': model, 'input_sha256': sha256(measured_input(original))}
        metadata = canonical_json({'format': 'reveal.dispatch-budget/1', 'view_format': VIEW_FORMAT,
                    'package_sha256': sha256(original), 'view_sha256': sha256(view),
                    'prompt_sha256': sha256(prompt.encode()), 'measurement': measurement})
        (self.root / VIEW_FILENAME).write_bytes(view); (self.root / BUDGET_FILENAME).write_bytes(metadata)
        saved = {'format': 'reveal.dispatch-input/1', 'binding': {'original_sha256': sha256(original),
                 'mode': 'box', 'model': model, 'budget': 24000}, 'path': 'package/evidence-package.json',
                 'sha256': sha256(original), 'measurement': measurement,
                 'dispatch_view': {'path': VIEW_FILENAME, 'sha256': sha256(view),
                    'budget_path': BUDGET_FILENAME, 'budget_sha256': sha256(metadata)}}
        (self.root / 'dispatch-input.json').write_bytes(canonical_json(saved))
        chosen, restored, legacy = fit_input_budget(self.path, 'box', 24000)
        self.assertEqual(chosen.read_bytes(), original); self.assertEqual(restored, package)
        self.assertEqual(legacy['count'], 12000)
        (self.root / VIEW_FILENAME).write_text('{}')
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)

    def test_concurrent_preparation_publishes_one_identical_full_file(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch('httpx.post', side_effect=AssertionError('No export')), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: fit_input_budget(self.path, 'box', 24000), range(2)))
        self.assertEqual(results[0], results[1])
        manifest = json.loads((self.root / 'dispatch-input.json').read_bytes())
        self.assertEqual(manifest['sha256'], sha256(self.path.read_bytes()))
        self.assertEqual(manifest['path'], 'package/evidence-package.json')


if __name__ == '__main__': unittest.main()
