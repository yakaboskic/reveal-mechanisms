"""Original publication recovery must preserve scientific bytes and boundaries."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('migrate_local_artifacts', ROOT / 'scripts/migrate_local_artifacts.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_publication_storage_references_change_without_altering_scientific_document():
    original = {'document': {'files': [{'id': 'scientific-file', 'sha256': 'abc', 'filename': 'source.json'}]},
                'artifact_records': {'abc': {'path': '/app/.runtime/artifacts/job/source.json',
                    'sha256': 'abc', 'file': {'id': 'scientific-file'}}}}
    changed = deepcopy(original)
    refs = list(migration.references(changed))
    assert len(refs) == 1
    refs[0]['storage'] = {'store': 's3', 'sha256': 'abc'}
    del refs[0]['path']
    assert changed['document'] == original['document']
    assert original['artifact_records']['abc']['path'].endswith('source.json')
    assert list(migration.references(changed)) == []


def test_container_artifacts_map_to_original_directory_and_reject_escapes(tmp_path):
    root = tmp_path / 'artifacts'; root.mkdir()
    (root / 'job').mkdir()
    path = root / 'job/source.json'; path.write_bytes(b'original')
    assert migration.local_path('/app/.runtime/artifacts/job/source.json', root) == path
    other = tmp_path / 'outside.json'; other.write_bytes(b'private')
    for value in ('/app/.runtime/artifacts/../outside.json', str(other), str(root / 'missing')):
        with pytest.raises(RuntimeError, match='missing or outside'): migration.local_path(value, root)
