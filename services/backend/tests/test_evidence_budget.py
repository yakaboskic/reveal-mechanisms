import shutil
import io
import json
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.evidence_budget import fit_input_budget
from reveal_backend.evidence_package import EvidenceBuildError, sha256
from reveal_backend.dispatch_view import (BUDGET_FILENAME, VIEW_FILENAME, VIEW_FORMAT, dispatch_view,
                                          measured_input, research_prompt, validate_dispatch_budget)
import test_evidence_package as fixtures


class BudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'capture'
        shutil.copytree(fixtures.EvidencePackageTests.root / 'capture', self.root)
        self.path = self.root / 'package/evidence-package.json'

    def test_reduction_preserves_sources_anchors_and_freezes_exact_result(self):
        originals = {str(p.relative_to(self.root)): sha256(p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        original_package = fixtures.EvidencePackageTests.built.package
        with patch('reveal_backend.evidence_budget.count_tokens', side_effect=[100000, 12000]):
            chosen, package, measurement = fit_input_budget(self.path, 'box', 24000)
        self.assertTrue(measurement['reduced'])
        self.assertEqual(package['selection'], original_package['selection'])
        self.assertEqual(package['source_artifacts'], original_package['source_artifacts'])
        self.assertLess(package['coverage']['retained_nodes'], original_package['coverage']['retained_nodes'])
        self.assertTrue(set(package['coverage']['omitted_node_ids']) >= set(original_package['coverage']['omitted_node_ids']))
        for relative, digest in originals.items(): self.assertEqual(sha256((self.root / relative).read_bytes()), digest)
        with patch('reveal_backend.evidence_budget.count_tokens', side_effect=AssertionError('Recovery must reuse exact measured input')):
            self.assertEqual(fit_input_budget(self.path, 'box', 24000), (chosen, package, measurement))
        chosen.write_text('{}')
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)

    def test_budget_or_execution_mode_cannot_silently_change_after_freeze(self):
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=1000):
            fit_input_budget(self.path, 'box', 24000)
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 30000)
        with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'deterministic', 24000)

    def test_initial_view_defers_only_file_catalogues_without_changing_science(self):
        from copy import deepcopy
        original_bytes = self.path.read_bytes()
        original = json.loads(original_bytes)
        view_bytes = dispatch_view(original_bytes)
        view = json.loads(view_bytes)
        expected = deepcopy(original)
        expected.pop('source_artifacts')
        expected['dapper_context'].pop('files')
        self.assertEqual(view['evidence'], expected)
        self.assertEqual(view['format'], VIEW_FORMAT)
        self.assertEqual(view['canonical_package']['sha256'], sha256(original_bytes))
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(measured_input(original_bytes),
                         (research_prompt(original['external_evidence']['selected_graphs']) + '\n\n').encode() + view_bytes)

    def test_measures_and_freezes_exact_view_plus_instructions_not_replacement_package(self):
        original = self.path.read_bytes()
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=12000) as count:
            chosen, package, measurement = fit_input_budget(self.path, 'box', 24000)
        count.assert_called_once_with(measured_input(original), measurement['model'])
        self.assertEqual(chosen, self.path.resolve())
        self.assertEqual(chosen.read_bytes(), original)
        self.assertEqual(package, json.loads(original))
        budget = json.loads((self.root / BUDGET_FILENAME).read_bytes())
        view = (self.root / VIEW_FILENAME).read_bytes()
        validate_dispatch_budget(original, view, budget,
                                 research_prompt(package['external_evidence']['selected_graphs']), measurement['model'])
        for changed in ('prompt', 'package', 'view', 'model'):
            with self.subTest(changed=changed), self.assertRaises(EvidenceBuildError):
                validate_dispatch_budget(original + (b' ' if changed == 'package' else b''),
                    view + (b' ' if changed == 'view' else b''), budget,
                    research_prompt(package['external_evidence']['selected_graphs']) + (' changed' if changed == 'prompt' else ''),
                    'changed-model' if changed == 'model' else measurement['model'])

    def test_bundle_preserves_all_source_bytes_and_rejects_missing_frozen_view_on_resume(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import BoxConfigurationError, make_bundle
        from reveal_backend.runtime_config import ROOT
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=12000):
            chosen, package, _ = fit_input_budget(self.path, 'box', 24000)
        request = ExecutionRequest('test-job', 1, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT, request)), mode='r:gz') as archive:
            self.assertEqual(archive.extractfile('input/evidence-package.json').read(), chosen.read_bytes())
            self.assertEqual(archive.extractfile('input/' + VIEW_FILENAME).read(), dispatch_view(chosen.read_bytes()))
            for item in package['source_artifacts'].values():
                self.assertEqual(archive.extractfile('input/' + item['path']).read(),
                                 (chosen.parent / item['path']).read_bytes())
        (self.root / VIEW_FILENAME).unlink()
        (self.root / BUDGET_FILENAME).unlink()
        with self.assertRaises(BoxConfigurationError): make_bundle(ROOT, request)

    def test_recovery_rejects_changed_view_without_remeasuring(self):
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=12000):
            chosen, _, _ = fit_input_budget(self.path, 'box', 24000)
        (self.root / VIEW_FILENAME).write_text('{}')
        with patch('reveal_backend.evidence_budget.count_tokens', side_effect=AssertionError('No remeasurement')):
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)

    def test_feedback_is_measured_explicitly_and_cannot_change_after_freeze(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import make_bundle
        from reveal_backend.runtime_config import ROOT
        feedback = ('Keep the causal alternatives unresolved unless supported by a cited source.',)
        original = self.path.read_bytes()
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=12000) as count:
            chosen, package, measurement = fit_input_budget(self.path, 'box', 24000, feedback)
        count.assert_called_once_with(measured_input(original, feedback), measurement['model'])
        request = ExecutionRequest('repair-job', 2, 'research', chosen, self.root / 'output',
                                   selected_graphs=tuple(package['external_evidence']['selected_graphs']), validation_feedback=feedback)
        self.assertTrue(make_bundle(ROOT, request))
        with patch('reveal_backend.evidence_budget.count_tokens', side_effect=AssertionError('Frozen input cannot be remeasured')):
            self.assertEqual(fit_input_budget(self.path, 'box', 24000, feedback)[2], measurement)
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000, ('Other feedback.',))

    def test_legacy_frozen_count_keeps_its_original_scope(self):
        from reveal_backend.evidence_package import canonical_json
        from reveal_backend.runtime_config import setting
        original = self.path.read_bytes()
        saved = {'format': 'reveal.dispatch-input/1', 'binding': {'original_sha256': sha256(original),
                 'mode': 'box', 'model': setting('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6'), 'budget': 24000},
                 'path': 'package/evidence-package.json', 'sha256': sha256(original),
                 'measurement': {'enforced': True, 'count': 12000, 'budget': 24000}}
        manifest = canonical_json(saved)
        (self.root / 'dispatch-input.json').write_bytes(manifest)
        with patch('reveal_backend.evidence_budget.count_tokens', side_effect=AssertionError('No change to legacy capture')):
            chosen, _, measured = fit_input_budget(self.path, 'box', 24000)
        self.assertEqual(chosen.read_bytes(), original)
        self.assertIn('were not measured', measured['scope'])
        self.assertFalse((self.root / BUDGET_FILENAME).exists())
        self.assertEqual((self.root / 'dispatch-input.json').read_bytes(), manifest)

    def test_irreducible_oversize_rejects_without_dispatch(self):
        with patch('reveal_backend.evidence_budget.count_tokens', return_value=10000000):
            with self.assertRaises(EvidenceBuildError): fit_input_budget(self.path, 'box', 24000)
        self.assertFalse((self.root / 'dispatch-input.json').exists())
        self.assertTrue((self.root / 'dispatch-budget-failure.json').exists())

    def test_concurrent_processes_reuse_one_frozen_reduced_package(self):
        import json
        import subprocess
        import sys
        import time
        source_root = str(Path(__file__).resolve().parents[1] / 'src')
        program = '''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from reveal_backend import evidence_budget
from reveal_backend.evidence_package import sha256
package=Path(sys.argv[2]); root=package.parent.parent; name=sys.argv[3]
original=package.read_bytes()
def count(data,model):
    with (root/'measurements.jsonl').open('a') as log:
        log.write(json.dumps({'process':name,'sha256':sha256(data)})+'\\n')
    if json.loads(data.decode().split('\\n\\n')[-1])['canonical_package']['sha256']==sha256(original):
        (root/(name+'.measuring')).touch()
        deadline=time.monotonic()+15
        while not (root/'release-measurement').exists():
            if time.monotonic()>deadline: raise TimeoutError('test measurement barrier')
            time.sleep(.01)
        return 100000
    return 12000
evidence_budget.count_tokens=count
(root/(name+'.started')).touch()
chosen,package,measurement=evidence_budget.fit_input_budget(package,'box',24000)
(root/(name+'.result.json')).write_text(json.dumps({'path':str(chosen),'sha256':sha256(chosen.read_bytes()),'measurement':measurement},sort_keys=True))
'''
        processes = []
        def cleanup():
            for process in processes:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)
        self.addCleanup(cleanup)
        def start(name):
            process = subprocess.Popen([sys.executable, '-c', program, source_root, str(self.path), name],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
        def wait_for(name):
            deadline = time.monotonic() + 15
            while not (self.root / name).exists():
                self.assertLess(time.monotonic(), deadline, 'Child process did not reach the test barrier')
                time.sleep(.01)
        start('first'); wait_for('first.measuring')
        start('second'); wait_for('second.started')
        # First process is held inside measurement. The second must wait at
        # the capture lock rather than measure or publish its own copy.
        time.sleep(.3)
        self.assertFalse((self.root / 'second.measuring').exists())
        (self.root / 'release-measurement').touch()
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
        first = json.loads((self.root / 'first.result.json').read_text())
        second = json.loads((self.root / 'second.result.json').read_text())
        self.assertEqual(first, second)
        self.assertTrue(first['measurement']['reduced'])
        measurements = [json.loads(line) for line in (self.root / 'measurements.jsonl').read_text().splitlines()]
        self.assertEqual(len(measurements), 2)  # Original and reduced, measured once each.
        self.assertEqual({row['process'] for row in measurements}, {'first'})
        frozen = json.loads((self.root / 'dispatch-input.json').read_text())
        self.assertEqual(frozen['sha256'], first['sha256'])


if __name__ == '__main__': unittest.main()
