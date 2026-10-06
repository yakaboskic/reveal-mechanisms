"""New authoring uses 0.2.0; historical identity replay remains isolated."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reveal_backend.evidence_package import sha256
from reveal_backend.runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT, dapper_snapshot_for_pin


class NewDapperReleaseTests(unittest.TestCase):
    def isolated(self, program, *arguments):
        prelude = '''import json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path[:0]=[str(root/'services/backend/src'),str(root/'services/backend/tests')]
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', prelude+program, str(ROOT), *arguments],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        return json.loads(result.stdout)

    def test_public_factor_resolution_uses_current_release_and_keeps_process_guard(self):
        result = self.isolated('''from reveal_backend.acceptance import public_runtime
from reveal_backend.evidence_package import DapperRuntime, EvidenceBuildError
from reveal_backend import runtime_config, catalog, research_work, worker, reference_reload
import test_research_data as fixture
runtime=public_runtime()
assert runtime.schema_path == runtime_config.CURRENT_DAPPER_SNAPSHOT/'snapshot/schema'
assert 'embeddings' in runtime.groups
for module in (catalog,research_work,worker):
    assert module.CURRENT_DAPPER_SNAPSHOT == runtime_config.CURRENT_DAPPER_SNAPSHOT
assert reference_reload.DEFAULT_DAPPER == runtime_config.CURRENT_DAPPER_SNAPSHOT
test=fixture.ReferenceQueryTests();test.setUp()
try:
    capture=test.query('get_factor',{'factor_id':fixture.FACTOR})
    context=capture.materialize(runtime)
    runtime.validate(context['dapper_context'])
    assert context['object_resolution'][0]['status']=='ready'
    mechanism=context['dapper_context']['mechanisms'][0]
    assert runtime.compute_id(mechanism,'Mechanism',runtime.schema)==mechanism['id']
finally: test.doCleanups()
try:
    DapperRuntime(root/'data/dapper/2026-09-24-v8')
except EvidenceBuildError as error:
    assert 'separate process' in str(error)
else: raise AssertionError('Mixed snapshot process was allowed')
print(json.dumps({'tag':runtime.manifest['tag'],'mechanism':mechanism['id']}))
''')
        self.assertEqual(result['tag'], '0.2.0')
        self.assertTrue(result['mechanism'].startswith('dapper:Mechanism.'))

    def test_historical_package_identity_replay_matches_in_separate_runtimes(self):
        program = '''from reveal_backend.evidence_package import DapperRuntime
from reveal_backend.runtime_config import dapper_snapshot_for_pin
package=json.loads((root/'docs/examples/evidence-package-cad/evidence-package.json').read_bytes())
selected=dapper_snapshot_for_pin(package['dapper_pin']) if sys.argv[2]=='historical' else root/'data/dapper/0.2.0'
runtime=DapperRuntime(selected)
nodes=runtime.validate(package['dapper_context'])
print(json.dumps({'ids':sorted(nodes),'snapshot':selected.name}))
'''
        historical = self.isolated(program, 'historical')
        current = self.isolated(program, 'current')
        self.assertEqual(historical['snapshot'], '2026-09-24-v8')
        self.assertEqual(current['snapshot'], '0.2.0')
        self.assertEqual(historical['ids'], current['ids'])
        self.assertTrue(historical['ids'])

    def test_snapshot_selection_requires_approved_hash_and_original_manifest(self):
        for directory in (CURRENT_DAPPER_SNAPSHOT, ROOT/'data/dapper/2026-09-24-v8'):
            with self.subTest(directory=directory.name):
                raw = (directory/'snapshot.json').read_bytes()
                pin = {'snapshot_sha256':json.loads(raw)['snapshot_sha256'], 'snapshot_manifest_sha256':sha256(raw)}
                self.assertEqual(dapper_snapshot_for_pin(pin), directory)
                with self.assertRaisesRegex(ValueError, 'original pin'):
                    dapper_snapshot_for_pin({**pin, 'snapshot_manifest_sha256':'f'*64})
        with self.assertRaisesRegex(ValueError, 'not approved'):
            dapper_snapshot_for_pin({'snapshot_sha256':'f'*64})

    def test_new_rehearsal_freezes_current_snapshot_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)/'workspace'
            result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/create_agent_rehearsal.py'),
                                     '--output', str(workspace)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
            package = json.loads((workspace/'input/evidence-package.json').read_bytes())
            manifest = json.loads((CURRENT_DAPPER_SNAPSHOT/'snapshot.json').read_bytes())
            self.assertEqual(package['dapper_pin']['snapshot_sha256'], manifest['snapshot_sha256'])
            self.assertEqual(package['authoring_kit']['release_tag'], '0.2.0')
            self.assertEqual(package['authoring_kit']['release_commit'], manifest['base_commit'])


if __name__ == '__main__': unittest.main()
