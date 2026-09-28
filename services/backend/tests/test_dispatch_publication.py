"""Interrupted publication never changes canonical evidence or source files."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import evidence_budget
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import BoxConfigurationError, make_bundle
from reveal_backend.dispatch_view import FILE_INPUT_FILENAME
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.runtime_config import ROOT
import test_evidence_package as fixtures


class DispatchPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'capture').resolve()
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', self.root)
        self.path = self.root / 'package/evidence-package.json'

    def test_replay_recovers_crash_after_sidecar_before_manifest(self):
        original = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        publish = evidence_budget.atomic_json
        def interrupted(path, value):
            if Path(path) == self.root / 'dispatch-input.json': raise OSError('Simulated process loss')
            return publish(path, value)
        with patch.object(evidence_budget, 'atomic_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'Simulated process loss'):
                evidence_budget.fit_input_budget(self.path, 'box', 24000)
        self.assertFalse((self.root / 'dispatch-input.json').exists())
        self.assertTrue((self.root / FILE_INPUT_FILENAME).is_file())
        self.assertFalse((self.path.parent / FILE_INPUT_FILENAME).exists())
        chosen, _, prepared = evidence_budget.fit_input_budget(self.path, 'box', 24000)
        self.assertEqual(chosen, self.path); self.assertFalse(prepared['reduced'])
        for name, raw in original.items(): self.assertEqual((self.root / name).read_bytes(), raw, name)
        self.assertTrue((self.root / 'dispatch-input.json').is_file())

    def test_frozen_manifest_path_cannot_escape_capture_in_preparation_or_bundle(self):
        chosen, package, _ = evidence_budget.fit_input_budget(self.path, 'box', 24000)
        manifest = self.root / 'dispatch-input.json'; saved = json.loads(manifest.read_bytes())
        request = ExecutionRequest('offline-publication', 1, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        (self.root.parent / FILE_INPUT_FILENAME).write_bytes((self.root / FILE_INPUT_FILENAME).read_bytes())
        saved['file_input']['path'] = '../' + FILE_INPUT_FILENAME
        evidence_budget.atomic_json(manifest, saved)
        with self.assertRaises(EvidenceBuildError): evidence_budget.fit_input_budget(self.path, 'box', 24000)
        with self.assertRaises(BoxConfigurationError): make_bundle(ROOT, request)


if __name__ == '__main__': unittest.main()
