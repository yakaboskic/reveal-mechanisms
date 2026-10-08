"""Provisioning the locked DAPPER release from a local source: a fresh clone, a no-op when current,
a stale checkout swapped and kept as <root>-<old ref>, and failures that leave the checkout untouched."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from reveal_backend import dapper_release
from reveal_backend.dapper_release import clone_release, ensure_release, verified_release, verify_release
from reveal_backend.evidence_package import EvidenceBuildError, sha256

ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = 'https://github.com/example/public-dapper.git'  # recorded as origin, never contacted


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def tag_release(source, tag, text, lock):
    (source / 'schema').mkdir(exist_ok=True); (source / 'schema/dapper.yaml').write_text(text)
    identity = ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid']
    git(source, 'add', 'schema'); git(source, *identity, 'commit', '-qm', tag); git(source, *identity, 'tag', '-am', tag, tag)
    lock.write_text(json.dumps({'lock_version': 'reveal.dapper-release/1', 'repository': REPOSITORY, 'tag': tag,
        'tag_object': git(source, 'rev-parse', 'refs/tags/' + tag), 'commit': git(source, 'rev-parse', 'HEAD'),
        'files': {'schema/dapper.yaml': sha256(text.encode())}, 'compatible_input_snapshots': []}))
    return lock


@pytest.fixture
def source(tmp_path):
    """A local DAPPER checkout at the newer of two tagged releases, with untracked and hook files that must not travel."""
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    tag_release(source, 'old-1', 'version: old\n', tmp_path / 'old.json')
    tag_release(source, 'new-2', 'version: new\n', tmp_path / 'new.json')
    git(source, 'remote', 'add', 'origin', REPOSITORY)
    (source / '.env').write_text('PRIVATE_SENTINEL'); (source / '.git/hooks/local-secret').write_text('PRIVATE_SENTINEL')
    return source


def stale(source, destination):
    """A checkout of the older release, as left behind by a previous lock."""
    subprocess.run(['git', 'clone', '--quiet', '--no-local', '--depth', '1', '--branch', 'old-1', '--', str(source), str(destination)], check=True)
    git(destination, 'remote', 'set-url', 'origin', REPOSITORY)
    return git(destination, 'rev-parse', 'HEAD')


def siblings(path): return sorted(p.name for p in path.parent.iterdir())


def test_missing_checkout_is_cloned_from_the_local_source_without_untracked_files_or_hooks(source, tmp_path):
    destination = tmp_path / 'runtime/dapper'
    release = ensure_release(destination, tmp_path / 'new.json', source)
    assert release['commit'] == git(source, 'rev-parse', 'new-2^{commit}') and 'previous' not in release
    assert git(destination, 'remote', 'get-url', 'origin') == REPOSITORY
    assert not (destination / '.env').exists() and not (destination / '.git/hooks/local-secret').exists()
    assert siblings(destination) == ['dapper']


def test_current_checkout_is_a_no_op(source, tmp_path, monkeypatch):
    destination = tmp_path / 'dapper'; clone_release(destination, tmp_path / 'new.json', source)
    before = destination.stat().st_ino, siblings(destination)
    monkeypatch.setattr(dapper_release, 'clone_release', lambda *args: pytest.fail('A current checkout must not be recloned'))
    assert ensure_release(destination, tmp_path / 'new.json', source) == verify_release(destination, tmp_path / 'new.json')
    assert (destination.stat().st_ino, siblings(destination)) == before


def test_stale_checkout_is_swapped_and_kept_under_its_old_ref(source, tmp_path):
    destination, lock = tmp_path / 'dapper', tmp_path / 'new.json'
    old = stale(source, destination)
    release = ensure_release(destination, lock, source)
    assert release['previous'] == str(tmp_path / 'dapper-old-1')
    assert verify_release(destination, lock)['commit'] == git(source, 'rev-parse', 'new-2^{commit}')
    assert git(tmp_path / 'dapper-old-1', 'rev-parse', 'HEAD') == old
    assert verified_release(tmp_path / 'dapper-old-1', tmp_path / 'old.json')
    assert not [name for name in siblings(destination) if name.startswith('.')]
    # A later stale checkout of the same ref never overwrites the kept copy.
    shutil.rmtree(destination); stale(source, destination)
    assert ensure_release(destination, lock, source)['previous'] == str(tmp_path / 'dapper-old-1.2')
    assert git(tmp_path / 'dapper-old-1', 'rev-parse', 'HEAD') == old


@pytest.mark.parametrize('failure', ['source', 'clone'])
def test_failed_verification_leaves_the_stale_checkout_untouched(source, tmp_path, failure):
    destination = tmp_path / 'dapper'; old = stale(source, destination)
    lock = json.loads((tmp_path / 'new.json').read_text()); lock['files']['schema/dapper.yaml'] = '0' * 64
    if failure == 'clone': lock['repository'] = str(source)  # cloned without a source check, then rejected
    (tmp_path / 'bad.json').write_text(json.dumps(lock))
    before = siblings(destination)
    with pytest.raises(EvidenceBuildError, match='checksum mismatch'):
        ensure_release(destination, tmp_path / 'bad.json', source if failure == 'source' else None)
    assert git(destination, 'rev-parse', 'HEAD') == old and siblings(destination) == before


def test_interrupted_swap_restores_the_original_checkout(source, tmp_path, monkeypatch):
    destination = tmp_path / 'dapper'; old = stale(source, destination); rename = Path.rename
    def failing(self, target):  # the staged clone cannot take the original's place; the rollback still can
        if Path(target) == destination and self.parent != destination.parent: raise OSError('interrupted swap')
        return rename(self, target)
    monkeypatch.setattr(Path, 'rename', failing)
    with pytest.raises(OSError, match='interrupted swap'): ensure_release(destination, tmp_path / 'new.json', source)
    monkeypatch.undo()
    assert git(destination, 'rev-parse', 'HEAD') == old
    assert not (tmp_path / 'dapper-old-1').exists() and not [n for n in siblings(destination) if n.startswith('.')]


@pytest.fixture
def local_sources(source, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import local_sources
    monkeypatch.setattr(local_sources, 'ASSET_DAPPER', source)
    return local_sources


def test_local_sources_upgrades_only_the_managed_checkout(local_sources, source, tmp_path, monkeypatch, capsys):
    managed, custom, lock = tmp_path / 'runtime/dapper', tmp_path / 'custom/dapper', tmp_path / 'new.json'
    monkeypatch.setattr(local_sources, 'MANAGED_DAPPER', managed)
    managed.parent.mkdir(); stale(source, managed); old = stale(source, custom)
    release = local_sources.provision_dapper(managed, lock)
    assert release['previous'] == str(tmp_path / 'runtime/dapper-old-1') and verified_release(managed, lock)
    assert 'restart running services' in capsys.readouterr().out
    with pytest.raises(RuntimeError, match='REVEAL_DAPPER_ROOT=.*not the locked DAPPER release'):
        local_sources.provision_dapper(custom, lock)
    assert git(custom, 'rev-parse', 'HEAD') == old and siblings(custom) == ['dapper']
    # A missing custom root is still cloned, from the verified local asset.
    assert local_sources.provision_dapper(tmp_path / 'fresh/dapper', lock)['commit'] == release['commit']
