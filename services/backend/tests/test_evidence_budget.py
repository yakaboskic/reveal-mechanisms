import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.evidence_budget import fit_input_budget
from reveal_backend.evidence_package import EvidenceBuildError, sha256
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
    if data==original:
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
