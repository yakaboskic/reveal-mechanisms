"""Clone and verify the DAPPER release selected by the trusted worker."""
from importlib.metadata import version
from pathlib import Path
import subprocess

from .evidence_package import canonical_json, decode, require, sha256


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=180)
    require(result.returncode == 0, f'Git operation failed: {result.stderr.strip()}')
    return result.stdout.strip()


def verify_release(root, lock_path):
    root, lock_path = Path(root).resolve(), Path(lock_path).resolve()
    lock_bytes = lock_path.read_bytes(); lock = decode(lock_bytes)
    require(lock['lock_version'] == 'reveal.dapper-release/1', 'Unsupported DAPPER release lock')
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
    return {'repository': lock['repository'], 'tag': lock['tag'], 'commit': lock['commit'],
            'lock_sha256': sha256(lock_bytes), 'checked_files': len(lock['files'])}


def clone_release(destination, lock_path):
    destination = Path(destination).resolve(); lock = decode(Path(lock_path).read_bytes())
    require(not destination.exists(), 'Each agent start requires a fresh DAPPER clone; use a new workspace')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['git', 'clone', '--depth', '1', '--single-branch', '--branch', lock['tag'],
                             '--', lock['repository'], str(destination)], capture_output=True, text=True, timeout=240)
    require(result.returncode == 0, f'DAPPER release clone failed: {result.stderr.strip()}')
    return verify_release(destination, lock_path)


def prepare_agent_workspace(workspace, project_root, package_path, lock_path):
    """Mandatory startup preparation, reusable by the future Box worker.

    Dependencies are installed in the worker/Box image, never by the agent.
    The caller owns mounting this release and input bundle read-only in Box.
    """
    workspace, project_root = Path(workspace).resolve(), Path(project_root).resolve()
    package_path, lock_path = Path(package_path).resolve(), Path(lock_path).resolve()
    require(not workspace.exists(), 'Agent workspace already exists; use a new directory per start')
    package = decode(package_path.read_bytes(), 'yaml' if package_path.suffix in ('.yaml', '.yml') else 'json')
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
                     'services/backend/src/reveal_backend/__init__.py',
                     'services/backend/src/reveal_backend/evidence_package.py',
                     'services/backend/src/reveal_backend/dapper_release.py',
                     'services/backend/src/reveal_backend/scientific_account_lint.py',
                     'services/backend/agent-skills/construct-scientific-account/SKILL.md',
                     'docs/evidence-package.md', 'docs/scientific-account-construction.md', 'docs/pigean-claim-model.md',
                     'docs/dapper-integration.md', 'docs/agent-evidence-integration.md', 'docs/scientific-account-linting.md']
    copied = {}
    for relative in relative_files:
        target = project / relative; target.parent.mkdir(parents=True, exist_ok=True)
        data = lock_path.read_bytes() if relative.endswith('dapper-release.json') else (project_root / relative).read_bytes()
        target.write_bytes(data); copied[relative] = sha256(data)
    skill = project / '.claude/skills/construct-scientific-account/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill_source = project / 'services/backend/agent-skills/construct-scientific-account/SKILL.md'
    skill.write_text(skill_source.read_text().replace('../../../../docs/', '../../../docs/'))
    copied[str(skill.relative_to(project))] = sha256(skill.read_bytes())
    inputs = project / 'input'; inputs.mkdir()
    (inputs / 'evidence-package.json').write_bytes(canonical_json(package))
    for source in package['source_artifacts'].values():
        relative = Path(source['path']); origin = (package_path.parent / relative).resolve()
        target = (inputs / relative).resolve()
        require(origin.is_relative_to(package_path.parent) and target.is_relative_to(inputs), 'Source artifact path escapes package')
        data = origin.read_bytes()
        require(sha256(data) == source['sha256'], f'Source artifact checksum mismatch: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    instruction_updates = []
    for instruction in [package['authoring']['skill'], package['authoring']['contract'], *package['authoring']['references']]:
        current_hash = copied.get(instruction['path'])
        if current_hash and current_hash != instruction['sha256']:
            instruction_updates.append({'path': instruction['path'], 'package_sha256': instruction['sha256'],
                                        'runtime_sha256': current_hash})
    manifest = {'runtime_version': 'reveal.agent-runtime/1', 'dapper': release, 'python_dependencies': dependencies,
                'evidence_package_sha256': sha256(canonical_json(package)), 'bundle_files': copied,
                'authoring_instructions': {'source': 'trusted runtime bundle', 'updates_from_package': instruction_updates},
                'working_directory': str(project), 'dapper_root': str(workspace / 'dapper'),
                'evidence_package': str(inputs / 'evidence-package.json'),
                'release_lock': str(project / 'services/backend/agent-runtime/dapper-release.json')}
    (workspace / 'runtime.json').write_bytes(canonical_json(manifest))
    return manifest
