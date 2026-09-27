"""Interrupted dispatch publication preserves exact source bytes and safe replay."""
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import evidence_budget
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import BoxConfigurationError, make_bundle
from reveal_backend.dispatch_view import BUDGET_FILENAME, VIEW_FILENAME, dispatch_view
from reveal_backend.evidence_package import EvidenceBuildError, sha256
from reveal_backend.runtime_config import ROOT
import test_evidence_package as fixtures


class DispatchPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'capture').resolve()
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', self.root)
        self.path = self.root / 'package/evidence-package.json'

    def test_reduced_package_recovers_crash_after_sidecars_before_manifest(self):
        original = {str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob('*') if p.is_file()}
        publish = evidence_budget.atomic_json

        def interrupted(path, value):
            if Path(path) == self.root / 'dispatch-input.json':
                raise OSError('Simulated process loss before final manifest')
            return publish(path, value)

        with patch.object(evidence_budget, 'count_tokens', side_effect=[100000, 12000]), \
                patch.object(evidence_budget, 'atomic_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'Simulated process loss'):
                evidence_budget.fit_input_budget(self.path, 'box', 24000)
        self.assertFalse((self.root / 'dispatch-input.json').exists())
        self.assertTrue((self.root / VIEW_FILENAME).is_file())
        self.assertTrue((self.root / BUDGET_FILENAME).is_file())
        reduced = self.root / 'dispatch/package'
        before = {str(p.relative_to(reduced)): p.read_bytes() for p in reduced.rglob('*') if p.is_file()}
        self.assertNotIn(VIEW_FILENAME, before)
        self.assertNotIn(BUDGET_FILENAME, before)

        with patch.object(evidence_budget, 'count_tokens', side_effect=[100000, 12000]):
            chosen, package, measurement = evidence_budget.fit_input_budget(self.path, 'box', 24000)
        self.assertTrue(measurement['reduced'])
        self.assertEqual(chosen.parent, reduced)
        self.assertEqual(before, {str(p.relative_to(reduced)): p.read_bytes()
                                  for p in reduced.rglob('*') if p.is_file()})
        for name, raw in original.items():
            self.assertEqual((self.root / name).read_bytes(), raw, name)
        saved = json.loads((self.root / 'dispatch-input.json').read_bytes())
        self.assertEqual(saved['dispatch_view']['path'], VIEW_FILENAME)
        self.assertEqual(saved['dispatch_view']['budget_path'], BUDGET_FILENAME)
        request = ExecutionRequest('offline-publication', 1, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT, request)), mode='r:gz') as bundle:
            self.assertEqual(bundle.extractfile('input/evidence-package.json').read(), chosen.read_bytes())
            self.assertEqual(bundle.extractfile('input/' + VIEW_FILENAME).read(), dispatch_view(chosen.read_bytes()))
            for artifact in package['source_artifacts'].values():
                raw = bundle.extractfile('input/' + artifact['path']).read()
                self.assertEqual(sha256(raw), artifact['sha256'])
                self.assertEqual(raw, (chosen.parent / artifact['path']).read_bytes())
        with patch.object(evidence_budget, 'count_tokens', side_effect=AssertionError('Frozen replay cannot remeasure')):
            self.assertEqual(evidence_budget.fit_input_budget(self.path, 'box', 24000), (chosen, package, measurement))

    def test_frozen_sidecar_paths_cannot_escape_capture_on_fit_or_bundle(self):
        with patch.object(evidence_budget, 'count_tokens', return_value=12000):
            chosen, package, _ = evidence_budget.fit_input_budget(self.path, 'box', 24000)
        manifest = self.root / 'dispatch-input.json'
        saved = json.loads(manifest.read_bytes())
        request = ExecutionRequest('offline-publication', 1, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        for field in ('path', 'budget_path'):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(saved))
                filename = changed['dispatch_view'][field]
                (self.root.parent / filename).write_bytes((self.root / filename).read_bytes())
                changed['dispatch_view'][field] = '../' + filename
                evidence_budget.atomic_json(manifest, changed)
                with self.assertRaises(EvidenceBuildError):
                    evidence_budget.fit_input_budget(self.path, 'box', 24000)
                with self.assertRaises(BoxConfigurationError):
                    make_bundle(ROOT, request)


if __name__ == '__main__':
    unittest.main()
