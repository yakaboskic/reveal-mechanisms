"""Platform build exports contain pinned runtime inputs, never local credentials."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import certifi
import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('platform_bundle', ROOT / 'scripts/platform_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)
import platform_assets as assets
import prompt_guard_assets as guard

MODEL_FILES = {'config.json': b'{"id2label": {"0": "LABEL_0", "1": "LABEL_1"}}', 'model.onnx': b'onnx graph', 'tokenizer.json': b'{}',
               'LICENSE': b'Llama 4 Community License'}


def pinned_model(root, files=MODEL_FILES):
    """An operator's fetched and pinned classifier: the lock and the folder it describes (fake bytes)."""
    lock = json.loads((ROOT / bundle.PROMPT_GUARD_LOCK).read_text())
    lock.update(revision='c' * 40, source_files={'model.safetensors': 'd' * 64},
                files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    write(root, bundle.PROMPT_GUARD_LOCK, json.dumps(lock))
    for name, data in files.items():
        target = root / bundle.PROMPT_GUARD / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return lock


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def commit(root):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Fixture')
    return git(root, 'rev-parse', 'HEAD')


def write(root, relative, value):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value)
    return target


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    git(root, 'init', '-q')
    for path in bundle.REQUIRED:
        write(root, path, '{}\n')
    write(root, 'services/backend/Dockerfile', (ROOT / 'services/backend/Dockerfile').read_text())
    write(root, 'services/backend/agent-runtime/dapper-release.json', json.dumps({'commit': 'a' * 40}))
    write(root, 'data/dismech-gaps/2026-09-24/manifest.json', json.dumps({'source_commit': 'b' * 40}))
    write(root, '.gitignore', '.deployment-assets/\n')
    pinned_model(root)
    for path in ('services/backend/src/reveal_backend/app.py', 'api/openapi.json', 'schema/gateway.schema.json',
                 'docs/evidence-package.md', 'services/backend/csl/apa.csl',
                 'services/backend/agent-skills/example/SKILL.md', 'data/dapper/2026-09-24-v8/snapshot.json'):
        write(root, path, 'reviewed source\n')
    # Even an accidentally tracked credential or generated directory is excluded.
    for path in ('.env', '.runtime/private-job.json', '.venv/lib/private.py',
                 'services/frontend/.env.local', 'api/.env', 'docs/settings.env',
                 'schema/node_modules/tool.js', 'schema/prisma/generated/client.py',
                 'services/backend/src/__pycache__/app.pyc', 'docs/private.password.txt',
                 'data/evidence-packages/private/job.json', 'data/cfde-genesets/2026-09-24/records.jsonl.gz'):
        write(root, path, 'PRIVATE_SENTINEL')
    commit(root)
    return root


def test_fetch_export_is_allowlisted_hashed_and_self_contained(source, tmp_path):
    output = tmp_path / 'reveal'
    manifest = bundle.export(output, root=source, fetch_assets=True)
    assert manifest['source_commit'] == git(source, 'rev-parse', 'HEAD')
    assert manifest['source_dirty'] is False
    assert manifest['assets']['mode'] == 'fetch-at-build'
    assert manifest['assets']['rds_ca_sha256'] == assets.RDS_CA_SHA256
    # Public assets are fetched in Docker. The gated classifier cannot be (CI has no Hugging Face token), so the
    # operator's verified copy is the only asset the bundle carries.
    assert [path.name for path in (output / '.deployment-assets').iterdir()] == ['prompt-guard-2-22m']
    assert {path.name: path.read_bytes() for path in (output / bundle.PROMPT_GUARD).iterdir()} == MODEL_FILES
    assert manifest['assets']['prompt_guard'] == {'repository': 'meta-llama/Llama-Prompt-Guard-2-22M', 'revision': 'c' * 40,
                                                  'files': {name: hashlib.sha256(data).hexdigest() for name, data in MODEL_FILES.items()}}
    assert not (output / '.git').exists()
    assert not (output / 'services/frontend').exists()
    for relative, descriptor in manifest['files'].items():
        data = (output / relative).read_bytes()
        assert b'PRIVATE_SENTINEL' not in data
        assert descriptor == {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    assert (output / 'build-manifest.json').read_text() == json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    dockerfile = (output / 'Dockerfile').read_text()
    assert 'AS assets' in dockerfile
    assert 'RUN python scripts/platform_assets.py --output /assets' in dockerfile
    assert 'COPY --from=assets /assets/dapper /app/.runtime/dapper' in dockerfile
    assert [line for line in dockerfile.splitlines() if line.startswith('COPY .deployment-assets')] == \
        ['COPY .deployment-assets/prompt-guard-2-22m /app/models/prompt-guard-2-22m']
    assert "verify_model('/app/models/prompt-guard-2-22m')" in dockerfile
    assert 'COPY data/dapper/0.2.0 /app/data/dapper/0.2.0' in dockerfile
    assert 'COPY data/dapper/2026-09-24-v8 /app/data/dapper/2026-09-24-v8' in dockerfile
    assert all(name in manifest['files'] for name in ('data/dapper/0.2.0/snapshot.json', 'data/dapper/2026-09-24-v8/snapshot.json'))
    assert 'COPY data/gene-identity/hgnc-2026-10-06 /app/data/gene-identity/hgnc-2026-10-06' in dockerfile
    assert all('data/gene-identity/hgnc-2026-10-06/' + name in manifest['files']
               for name in ('manifest.json', 'hgnc_complete_set.tsv.gz', 'withdrawn.tsv.gz'))
    assert (output / 'services/backend/Dockerfile').read_text() == (source / 'services/backend/Dockerfile').read_text()
    # A second clean export has the same manifest, regardless of output path.
    assert bundle.export(tmp_path / 'again', root=source, fetch_assets=True) == manifest


def test_dirty_sources_require_explicit_flag_and_include_only_allowlisted_untracked_sources(source, tmp_path):
    write(source, 'services/backend/src/reveal_backend/app.py', 'reviewed local change')
    write(source, 'services/backend/src/reveal_backend/untracked.py', 'not staged')
    output = tmp_path / 'reveal'
    with pytest.raises(RuntimeError, match='Commit the source'):
        bundle.export(output, root=source, fetch_assets=True)
    assert not output.exists()
    manifest = bundle.export(output, root=source, fetch_assets=True, allow_dirty=True)
    assert manifest['source_dirty'] is True
    assert (output / 'services/backend/src/reveal_backend/app.py').read_text() == 'reviewed local change'
    assert (output / 'services/backend/src/reveal_backend/untracked.py').read_text() == 'not staged'
    assert not (output / '.env').exists()


def test_export_refuses_overwrite_and_symlink_inputs(source, tmp_path):
    output = tmp_path / 'reveal'
    output.mkdir()
    write(output, 'sentinel', 'preserve')
    with pytest.raises(FileExistsError, match='overwrite'):
        bundle.export(output, root=source, fetch_assets=True)
    assert (output / 'sentinel').read_text() == 'preserve'
    target = source / 'docs/evidence-package.md'
    target.unlink()
    target.symlink_to(source / '.env')
    with pytest.raises(RuntimeError, match='without symlinks'):
        bundle.export(tmp_path / 'other', root=source, fetch_assets=True, allow_dirty=True)
    assert not (tmp_path / 'other').exists()


def test_changed_dockerfile_and_missing_required_sources_fail_before_export(source, tmp_path):
    dockerfile = source / 'services/backend/Dockerfile'
    dockerfile.write_text(dockerfile.read_text().replace('COPY .deployment-assets/dapper ', 'COPY different/dapper '))
    with pytest.raises(RuntimeError, match='asset layout changed'):
        bundle.export(tmp_path / 'reveal', root=source, fetch_assets=True, allow_dirty=True)
    assert not (tmp_path / 'reveal').exists()
    git(source, 'rm', 'scripts/platform_assets.py')
    with pytest.raises(RuntimeError, match='stage them first'):
        bundle.export(tmp_path / 'reveal', root=source, fetch_assets=True, allow_dirty=True)


def test_local_asset_validation_failure_never_publishes_partial_output(source, tmp_path, monkeypatch):
    def reject(*args, **kwargs):
        raise RuntimeError('DAPPER pin mismatch')
    monkeypatch.setattr(bundle, 'prepare_assets', reject)
    with pytest.raises(RuntimeError, match='DAPPER pin mismatch'):
        bundle.export(tmp_path / 'reveal', root=source)
    assert not (tmp_path / 'reveal').exists()


@pytest.fixture
def asset_sources(tmp_path, monkeypatch):
    root, dapper, dismech = (tmp_path / name for name in ('source', 'dapper', 'dismech'))
    root.mkdir()
    dapper.mkdir()
    git(dapper, 'init', '-q')
    write(dapper, 'schema/dapper.yaml', 'prefixes: {}\n')
    dapper_commit = commit(dapper)
    git(dapper, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'tag', '-am', 'Release', 'test-release')
    repository = 'https://github.com/example/public-dapper.git'
    git(dapper, 'remote', 'add', 'origin', repository)
    lock = {'lock_version': 'reveal.dapper-release/1', 'repository': repository, 'tag': 'test-release',
        'commit': dapper_commit, 'tag_object': git(dapper, 'rev-parse', 'refs/tags/test-release'),
        'files': {'schema/dapper.yaml': hashlib.sha256((dapper / 'schema/dapper.yaml').read_bytes()).hexdigest()}}
    write(root, 'services/backend/agent-runtime/dapper-release.json', json.dumps(lock))
    write(dapper, '.env', 'PRIVATE_SENTINEL')
    write(dapper, '.git/hooks/local-secret', 'PRIVATE_SENTINEL')
    dismech.mkdir()
    git(dismech, 'init', '-q')
    schema = 'src/dismech/schema/dismech.yaml'
    write(dismech, schema, 'prefixes: {MONDO: test}\n')
    document = write(dismech, 'kb/disorders/example.yaml', 'name: Example\n')
    dismech_commit = commit(dismech)
    write(root, 'data/dismech-gaps/2026-09-24/manifest.json', json.dumps({'source_commit': dismech_commit}))
    write(root, 'data/dismech-gaps/2026-09-24/source-files.json', json.dumps([
        {'path': 'kb/disorders/example.yaml', 'sha256': hashlib.sha256(document.read_bytes()).hexdigest()}]))
    # The runtime schema must come from the pinned commit, not this working edit.
    write(dismech, schema, 'prefixes: {MONDO: changed}\n')
    certs = Path(certifi.where()).read_bytes()
    certificate = b'-----BEGIN CERTIFICATE-----' + certs.split(b'-----BEGIN CERTIFICATE-----', 1)[1].split(b'-----END CERTIFICATE-----', 1)[0] + b'-----END CERTIFICATE-----\n'
    ca = tmp_path / 'ca.pem'
    ca.write_bytes(certificate)
    monkeypatch.setattr(assets, 'RDS_CA_SHA256', hashlib.sha256(certificate).hexdigest())
    return root, dapper, dismech, ca


def test_local_assets_verify_pins_and_omit_untracked_files_and_git_hooks(asset_sources, tmp_path):
    root, dapper, dismech, ca = asset_sources
    output = tmp_path / 'assets'
    result = assets.prepare(output, root=root, dapper=dapper, dismech=dismech, ca=ca)
    assert result['dapper']['commit'] == git(dapper, 'rev-parse', 'HEAD')
    assert (output / 'dapper/.git/HEAD').is_file()
    assert not (output / 'dapper/.env').exists()
    assert not (output / 'dapper/.git/hooks/local-secret').exists()
    assert git(output / 'dapper', 'remote', 'get-url', 'origin') == 'https://github.com/example/public-dapper.git'
    assert (output / 'dismech/src/dismech/schema/dismech.yaml').read_text() == 'prefixes: {MONDO: test}\n'
    assert (output / 'dismech/kb/disorders/example.yaml').read_text() == 'name: Example\n'
    assert (output / 'rds-ca.pem').read_bytes() == ca.read_bytes()


@pytest.mark.parametrize('invalid', ['dapper', 'dismech', 'ca'])
def test_asset_tampering_is_rejected_without_leaving_output(asset_sources, tmp_path, invalid):
    root, dapper, dismech, ca = asset_sources
    path = {'dapper': dapper / 'schema/dapper.yaml', 'dismech': dismech / 'kb/disorders/example.yaml', 'ca': ca}[invalid]
    path.write_text('untrusted replacement')
    output = tmp_path / 'assets'
    with pytest.raises((ValueError, RuntimeError)):
        assets.prepare(output, root=root, dapper=dapper, dismech=dismech, ca=ca)
    assert not output.exists()


def test_invalid_fresh_ca_fails_before_any_git_asset_work(asset_sources, tmp_path, monkeypatch):
    root, _, _, _ = asset_sources
    commands = []
    def download_bad_ca(*command):
        commands.append(command)
        assert command[0] == 'curl', 'CA validation must happen before any Git work'
        Path(command[-1]).write_text('unreviewed certificate bundle')
    def forbid_clone(*args, **kwargs):
        pytest.fail('Invalid CA must prevent cloning DAPPER')
    monkeypatch.setattr(assets, 'run', download_bad_ca)
    monkeypatch.setattr(assets, 'clone_release', forbid_clone)
    output = tmp_path / 'assets'
    with pytest.raises(RuntimeError, match='RDS CA differs'):
        assets.prepare(output, root=root)
    assert len(commands) == 1 and commands[0][0] == 'curl'
    assert not output.exists()


def test_the_model_folder_must_match_its_pinned_lock_in_both_modes(source, tmp_path, monkeypatch):
    target = source / bundle.PROMPT_GUARD / 'model.onnx'
    target.write_bytes(b'other weights')
    with pytest.raises(guard.FetchError, match='HF_TOKEN'):
        bundle.export(tmp_path / 'reveal', root=source, fetch_assets=True)   # fetched again only with a token
    assert not (tmp_path / 'reveal').exists()
    monkeypatch.setattr(bundle, 'prepare_assets', lambda staging, **kwargs: Path(staging).mkdir() or {})
    with pytest.raises(guard.FetchError, match='does not hold the pinned'):
        bundle.export(tmp_path / 'local', root=source)
    assert not (tmp_path / 'local').exists()
    target.write_bytes(MODEL_FILES['model.onnx'])
    manifest = bundle.export(tmp_path / 'local', root=source)
    assert manifest['assets']['mode'] == 'verified-local' and manifest['assets']['prompt_guard']['revision'] == 'c' * 40
    assert (tmp_path / 'local' / bundle.PROMPT_GUARD / 'model.onnx').read_bytes() == MODEL_FILES['model.onnx']


class FakeHub:
    """huggingface.co for one gated repository: a listing and files, behind a token."""
    def __init__(self, files, sha='e' * 40):
        self.files, self.sha, self.urls = files, sha, []

    def __call__(self, url, token, target=None):
        assert token == 'hf_test_token'
        self.urls.append(url)
        if '/api/models/' in url:
            return json.dumps({'sha': self.sha, 'siblings': [{'rfilename': name} for name in [*self.files, 'README.md', '.gitattributes']]}).encode()
        Path(target).write_bytes(self.files[url.rsplit('/', 1)[1]])


def fake_conversion(source, output, lock, converter):
    output.mkdir()
    (output / 'model.onnx').write_bytes(b'onnx from ' + (source / 'model.safetensors').read_bytes())
    (output / 'tokenizer.json').write_bytes(b'{"fast": true}')
    return 'optimum-cli 9.9'


HUB_FILES = {'config.json': b'{"id2label": {"0": "LABEL_0", "1": "LABEL_1"}}', 'model.safetensors': b'weights',
             'tokenizer_config.json': b'{}', 'LICENSE': b'Llama 4 Community License', 'USE_POLICY.md': b'policy'}


def test_an_unpinned_lock_is_fetched_only_when_pinning_is_asked_for_and_then_pinned(tmp_path):
    lock_path = tmp_path / 'prompt-guard-release.json'
    lock_path.write_text((ROOT / bundle.PROMPT_GUARD_LOCK).read_text())
    destination = tmp_path / '.deployment-assets/prompt-guard-2-22m'
    hub = FakeHub(HUB_FILES)
    with pytest.raises(guard.FetchError, match='pins no revision'):
        guard.fetch(destination, lock_path, token='hf_test_token', get=hub, run_conversion=fake_conversion)
    with pytest.raises(guard.FetchError, match='HF_TOKEN'):
        guard.fetch(destination, lock_path, pin=True, get=hub, run_conversion=fake_conversion)
    assert hub.urls == [] and not destination.exists()
    result = guard.fetch(destination, lock_path, token='hf_test_token', pin=True, get=hub, run_conversion=fake_conversion)
    lock = json.loads(lock_path.read_text())
    assert (result['revision'], result['pinned'], lock['revision']) == ('e' * 40, True, 'e' * 40)
    # Only the files the conversion needs and the licence texts are downloaded, and each is pinned.
    assert set(lock['source_files']) == set(HUB_FILES)
    assert lock['source_files']['model.safetensors'] == hashlib.sha256(b'weights').hexdigest()
    assert all(url.startswith('https://huggingface.co/') for url in hub.urls) and not any('README' in url for url in hub.urls)
    assert sorted(path.name for path in destination.iterdir()) == ['LICENSE', 'USE_POLICY.md', 'config.json', 'model.onnx', 'tokenizer.json']
    assert lock['files']['model.onnx'] == hashlib.sha256(b'onnx from weights').hexdigest() and lock['conversion']['converter'] == 'optimum-cli 9.9'
    # The pinned folder is reused without the network; a missing folder is fetched at the pinned revision and verified.
    again = FakeHub({})
    assert guard.fetch(destination, lock_path, get=again, run_conversion=fake_conversion)['fetched'] is False and again.urls == []
    shutil.rmtree(destination)
    assert guard.fetch(destination, lock_path, token='hf_test_token', get=FakeHub(HUB_FILES), run_conversion=fake_conversion)['fetched'] is True
    from reveal_backend import deposit_screen
    assert deposit_screen.verify_model(destination, lock_path, loader=lambda folder, pinned: pinned['revision']) == 'e' * 40


def test_bytes_that_differ_from_the_lock_are_refused_and_nothing_changes(tmp_path):
    lock_path = tmp_path / 'prompt-guard-release.json'
    lock_path.write_text((ROOT / bundle.PROMPT_GUARD_LOCK).read_text())
    destination = tmp_path / 'model'
    guard.fetch(destination, lock_path, token='hf_test_token', pin=True, get=FakeHub(HUB_FILES), run_conversion=fake_conversion)
    pinned = lock_path.read_text()
    shutil.rmtree(destination)
    with pytest.raises(guard.FetchError, match='model.safetensors .* differs'):
        guard.fetch(destination, lock_path, token='hf_test_token', get=FakeHub({**HUB_FILES, 'model.safetensors': b'tampered'}),
                    run_conversion=fake_conversion)
    with pytest.raises(guard.FetchError, match='different commit'):
        guard.fetch(destination, lock_path, token='hf_test_token', get=FakeHub(HUB_FILES, sha='f' * 40), run_conversion=fake_conversion)

    def other_export(source, output, lock, converter):
        fake_conversion(source, output, lock, converter); (output / 'model.onnx').write_bytes(b'reconverted')
    with pytest.raises(guard.FetchError, match='converted model differs'):
        guard.fetch(destination, lock_path, token='hf_test_token', get=FakeHub(HUB_FILES), run_conversion=other_export)
    assert not destination.exists() and lock_path.read_text() == pinned


def test_the_token_goes_to_hugging_face_only(monkeypatch):
    with pytest.raises(guard.FetchError, match='Refusing to send'):
        guard.http_get('https://example.org/x', 'hf_test_token')
    import urllib.request
    handler = guard.SameHost()
    request = urllib.request.Request('https://huggingface.co/m/resolve/x/model.safetensors', headers={'Authorization': 'Bearer hf_test_token'})
    elsewhere = handler.redirect_request(request, None, 302, 'Found', {}, 'https://cdn-lfs.example.net/blob')
    same = handler.redirect_request(request, None, 302, 'Found', {}, 'https://huggingface.co/api/resolve-cache/x')
    assert not elsewhere.has_header('Authorization') and same.has_header('Authorization')


def test_pinning_needs_the_fetch_step(source, tmp_path):
    with pytest.raises(RuntimeError, match='needs --fetch-assets'):
        bundle.export(tmp_path / 'reveal', root=source, pin_prompt_guard=True)
