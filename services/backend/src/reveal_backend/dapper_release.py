"""Clone and verify the DAPPER release selected by the trusted worker."""
from importlib.metadata import version
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from .evidence_package import EvidenceBuildError, canonical_json, decode, require, sha256

# Per process: the git inputs last verified for each checkout. File contents are rehashed on every call.
_VERIFIED = {}
_VERIFIED_LOCK = threading.Lock()


def git(root, *args):
    # No optional locks: status never rewrites the checkout's index, so the verified inputs stay as read.
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=180,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    require(result.returncode == 0, f'Git operation failed: {result.stderr.strip()}')
    return result.stdout.strip()


def _stat(path):
    try: value = os.lstat(path)
    except OSError: return None
    return value.st_mtime_ns, value.st_ctime_ns, value.st_size, value.st_ino, value.st_dev, value.st_mode


def provenance_key(root, lock_bytes, lock):
    """Every local input of verify_release's git checks, or None when they cannot be enumerated.

    HEAD and the ref it names, the refs, config, index, ignore and attribute files
    and every entry under schema/: a change to any of them misses the cache.
    """
    git_dir = root / '.git'
    if git_dir.is_symlink() or not git_dir.is_dir(): return None
    try: head = (git_dir / 'HEAD').read_bytes()
    except OSError: return None
    paths = [git_dir / name for name in ('HEAD', 'packed-refs', 'config', 'index', 'shallow', 'info/exclude')]
    paths += [git_dir / 'refs/tags' / lock['tag'], root / '.gitignore', root / '.gitattributes']
    if head.startswith(b'ref: '): paths.append(git_dir / head[5:].strip().decode('utf-8', 'replace'))
    entries = tuple(sorted((str(p.relative_to(root)), _stat(p)) for p in (root / 'schema').rglob('*')))
    return sha256(lock_bytes), head, tuple(_stat(p) for p in paths), _stat(root / 'schema'), entries


def verify_release(root, lock_path):
    root, lock_path = Path(root).resolve(), Path(lock_path).resolve()
    lock_bytes = lock_path.read_bytes(); lock = decode(lock_bytes)
    require(lock['lock_version'] == 'reveal.dapper-release/1', 'Unsupported DAPPER release lock')
    # Taken before the checks, so a change made while they run can only cause a miss.
    key = provenance_key(root, lock_bytes, lock)
    with _VERIFIED_LOCK: known = key is not None and _VERIFIED.get(str(root)) == key
    if not known:
        require(git(root, 'rev-parse', 'HEAD') == lock['commit'], 'DAPPER checkout commit differs from release lock')
        require(git(root, 'rev-parse', 'refs/tags/' + lock['tag']) == lock['tag_object'], 'DAPPER release tag object differs')
        require(git(root, 'rev-parse', 'refs/tags/' + lock['tag'] + '^{commit}') == lock['commit'], 'DAPPER tag resolves to a different commit')
        require(git(root, 'remote', 'get-url', 'origin') == lock['repository'], 'DAPPER repository differs from release lock')
        require(not git(root, 'status', '--porcelain', '--untracked-files=all', '--', 'schema'), 'DAPPER schema checkout has local changes')
    actual = {str(p.relative_to(root)) for p in (root / 'schema').rglob('*')
              if p.is_file() and p.suffix in ('.py', '.yaml', '.yml', '.json', '.pyc')}
    require(actual == set(lock['files']), 'DAPPER runtime file set differs from release lock')
    for relative, expected in lock['files'].items():
        path = (root / relative).resolve()
        require(path.is_relative_to(root), 'DAPPER release file escapes checkout')
        require(sha256(path.read_bytes()) == expected, f'DAPPER release checksum mismatch: {relative}')
    if key is not None and not known:
        with _VERIFIED_LOCK:
            if len(_VERIFIED) >= 64: _VERIFIED.clear()
            _VERIFIED[str(root)] = key
    return {'repository': lock['repository'], 'tag': lock['tag'], 'commit': lock['commit'],
            'lock_sha256': sha256(lock_bytes), 'checked_files': len(lock['files'])}


def verified_release(root, lock_path):
    """verify_release's result, or None when root is absent or not the locked release."""
    try: return verify_release(root, lock_path)
    except (EvidenceBuildError, OSError): return None


def clone_release(destination, lock_path, source=None):
    """A fresh verified clone of the locked tag, from its repository or from a local checkout that verifies.

    A local source contributes only the pinned Git tree: never untracked files,
    hooks or credential-bearing Git configuration.
    """
    destination = Path(destination).resolve(); lock = decode(Path(lock_path).read_bytes())
    require(not destination.exists(), 'Each agent start requires a fresh DAPPER clone; use a new workspace')
    if source is not None: verify_release(source, lock_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    origin = lock['repository'] if source is None else str(Path(source).resolve())
    local = () if source is None else ('--quiet', '--no-local', '--no-hardlinks')
    result = subprocess.run(['git', '-c', 'init.templateDir=', 'clone', *local, '--depth', '1', '--single-branch',
                             '--branch', lock['tag'], '--', origin, str(destination)], capture_output=True, text=True, timeout=240)
    require(result.returncode == 0, f'DAPPER release clone failed: {result.stderr.strip()}')
    if source is not None: git(destination, 'remote', 'set-url', 'origin', lock['repository'])
    return verify_release(destination, lock_path)


def ensure_release(destination, lock_path, source=None):
    """Verify destination, replacing a missing or stale checkout with a verified clone of the locked release.

    The replacement is cloned and verified beside destination before the swap, so a
    failure leaves the existing checkout untouched; that checkout is kept as <destination>-<old ref>.
    """
    destination = Path(destination).absolute()
    if not os.path.lexists(destination): return clone_release(destination, lock_path, source)
    release = verified_release(destination, lock_path)
    if release is not None: return release
    try: ref = git(destination, 'describe', '--tags', '--always') if (destination / '.git').is_dir() else 'unverified'
    except EvidenceBuildError: ref = 'unverified'
    ref = re.sub(r'[^A-Za-z0-9._-]+', '_', ref) or 'unverified'
    backup, number = destination.with_name(f'{destination.name}-{ref}'), 1
    while os.path.lexists(backup): number += 1; backup = destination.with_name(f'{destination.name}-{ref}.{number}')
    with tempfile.TemporaryDirectory(prefix=f'.{destination.name}-', dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        clone_release(staged, lock_path, source)
        destination.rename(backup)
        try: staged.rename(destination)
        except BaseException: backup.rename(destination); raise
    return {**verify_release(destination, lock_path), 'previous': str(backup)}


def prepare_agent_workspace(workspace, project_root, package_path, lock_path):
    """Mandatory startup preparation, reusable by the future Box worker.

    Dependencies are installed in the worker/Box image, never by the agent.
    The caller owns mounting this release and input bundle read-only in Box.
    """
    workspace, project_root = Path(workspace).resolve(), Path(project_root).resolve()
    package_path, lock_path = Path(package_path).resolve(), Path(lock_path).resolve()
    require(not workspace.exists(), 'Agent workspace already exists; use a new directory per start')
    package_bytes = package_path.read_bytes()
    package = decode(package_bytes, 'yaml' if package_path.suffix in ('.yaml', '.yml') else 'json')
    if package_path.suffix in ('.yaml', '.yml'):
        package_bytes = canonical_json(package)
    lock = decode(lock_path.read_bytes())
    require(package['package_version'] == 'reveal.evidence-package/0.2-draft', 'Unsupported agent evidence package')
    require(package['dapper_pin']['snapshot_sha256'] in lock['compatible_input_snapshots'], 'Evidence-package DAPPER pin is not approved for this release')
    # Resolve dependencies now so a missing runtime cannot launch a paid agent.
    dependencies = {name: version(name) for name in ('PyYAML', 'linkml', 'linkml-runtime', 'rdflib')}
    require(dependencies['linkml'] == '1.11.1', 'The account linter requires LinkML 1.11.1')
    workspace.mkdir(parents=True)
    release = clone_release(workspace / 'dapper', lock_path)
    project = workspace / 'reveal'
    relative_files = ['scripts/lint_scientific_account.py', 'services/backend/agent-runtime/dapper-release.json',
                     'services/backend/agent-runtime/authoring-schema-dependencies.json',
                     'services/backend/agent-runtime/linkml-types-1.11.1.yaml',
                     'services/backend/src/reveal_backend/__init__.py',
                     'services/backend/src/reveal_backend/evidence_package.py',
                     'services/backend/src/reveal_backend/evidence_files.py',
                     'services/backend/src/reveal_backend/evidence_reader.py',
                     'services/backend/src/reveal_backend/authoring_contract.py',
                     'services/backend/agent-runtime/authoring-schema-excerpt.yaml',
                     'services/backend/agent-runtime/authoring-examples.json',
                     'docs/authoring-contract.md', 'docs/local-agent-mcp.md', 'docs/local-workspaces-and-authentication.md',
                     'services/backend/src/reveal_backend/dapper_release.py',
                     'services/backend/src/reveal_backend/scientific_account_lint.py',
                     'services/backend/src/reveal_backend/source_validation.py',
                     'services/backend/src/reveal_backend/relationship_provenance.py',
                     'services/backend/src/reveal_backend/claim_suggestions.py',
                     'services/backend/agent-skills/construct-scientific-account/SKILL.md',
                     'services/backend/agent-skills/read-evidence-package/SKILL.md',
                     'docs/evidence-package.md', 'docs/scientific-account-construction.md', 'docs/pigean-claim-model.md',
                     'docs/dapper-integration.md', 'docs/agent-evidence-integration.md', 'docs/scientific-account-linting.md']
    copied = {}
    for relative in relative_files:
        target = project / relative; target.parent.mkdir(parents=True, exist_ok=True)
        data = lock_path.read_bytes() if relative.endswith('dapper-release.json') else (project_root / relative).read_bytes()
        target.write_bytes(data); copied[relative] = sha256(data)
    inputs = project / 'input'; inputs.mkdir()
    (inputs / 'evidence-package.json').write_bytes(package_bytes)
    for source in package['source_artifacts'].values():
        relative = Path(source['path']); origin = (package_path.parent / relative).resolve()
        target = (inputs / relative).resolve()
        require(origin.is_relative_to(package_path.parent) and target.is_relative_to(inputs), 'Source artifact path escapes package')
        data = origin.read_bytes()
        require(sha256(data) == source['sha256'], f'Source artifact checksum mismatch: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    kit = package.get('authoring_kit', {})
    pinned = kit.get('version') == 'reveal.research-authoring-kit/2'
    if pinned:
        entries = kit.get('files')
        require(isinstance(entries, list) and sha256(canonical_json(entries)) == kit.get('kit_sha256'),
                'Frozen authoring kit manifest changed')
        # Pin scientific prose and schema/examples while keeping executable
        # runtime code supplied by the trusted deployment. Do not execute an
        # older copied implementation merely because it accompanies a seed.
        allowed = {name for name in relative_files if name.endswith(('.md', '.yaml', 'authoring-examples.json'))}
        allowed.update(('input/package-sections/authoring-schema-excerpt.yaml',
                        'input/package-sections/authoring-examples.json',
                        'services/backend/agent-runtime/authoring-schema-dependencies.json'))
        from .authoring_contract import SKELETON_PATH
        from .dispatch_view import pinned_skeleton_sha256
        skeleton_pin = pinned_skeleton_sha256(package)
        if skeleton_pin is not None: allowed.add(SKELETON_PATH)
        installed = {}
        for entry in entries:
            relative = entry.get('path')
            source = package['source_artifacts'].get(entry.get('artifact_id'), {})
            require(source.get('sha256') == entry.get('sha256'), 'Frozen authoring source binding changed')
            origin = (inputs / source.get('path', '')).resolve()
            require(origin.is_relative_to(inputs) and origin.is_file(), 'Frozen authoring source is unavailable')
            data = origin.read_bytes()
            require(sha256(data) == entry['sha256'] and len(data) == source.get('size_bytes'), 'Frozen authoring source changed')
            if relative == 'services/backend/agent-runtime/dapper-release.json':
                require(data == lock_path.read_bytes(), 'Frozen authoring release differs from the hosted validator release')
            if relative not in allowed: continue
            require(relative not in installed or installed[relative] == entry['sha256'], 'Conflicting frozen authoring path')
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            installed[relative] = copied[relative] = sha256(data)
        for relative in ({name for name in relative_files if name.endswith('.md')} |
                         {'input/package-sections/authoring-schema-excerpt.yaml', 'input/package-sections/authoring-examples.json'} |
                         ({SKELETON_PATH} if skeleton_pin is not None else set())):
            require(relative in installed, 'Frozen authoring kit is missing required file: '+relative)
    for name in ('construct-scientific-account', 'read-evidence-package'):
        skill = project / f'.claude/skills/{name}/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill_source = project / f'services/backend/agent-skills/{name}/SKILL.md'
        skill.write_text(skill_source.read_text().replace('../../../../docs/', '../../../docs/'))
        copied[str(skill.relative_to(project))] = sha256(skill.read_bytes())
    instruction_updates = []
    for instruction in [package['authoring']['skill'], package['authoring']['contract'], *package['authoring']['references']]:
        current_hash = copied.get(instruction['path'])
        if current_hash and current_hash != instruction['sha256']:
            instruction_updates.append({'path': instruction['path'], 'package_sha256': instruction['sha256'],
                                        'runtime_sha256': current_hash})
    manifest = {'runtime_version': 'reveal.agent-runtime/1', 'dapper': release, 'python_dependencies': dependencies,
                'evidence_package_sha256': sha256(package_bytes), 'bundle_files': copied,
                'authoring_instructions': {'source': 'frozen authoring kit' if pinned else 'trusted runtime bundle',
                    'kit_sha256': kit.get('kit_sha256') if pinned else None, 'updates_from_package': instruction_updates},
                'working_directory': str(project), 'dapper_root': str(workspace / 'dapper'),
                'evidence_package': str(inputs / 'evidence-package.json'),
                'release_lock': str(project / 'services/backend/agent-runtime/dapper-release.json')}
    (workspace / 'runtime.json').write_bytes(canonical_json(manifest))
    return manifest
