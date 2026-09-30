#!/usr/bin/env python3
"""Export only reviewed backend build inputs for a DIG platform service directory."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

from dotenv import dotenv_values
from platform_assets import prepare as prepare_assets, RDS_CA_SHA256


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    'services/backend/src/', 'services/backend/agent-runtime/',
    'services/backend/agent-skills/', 'services/backend/csl/',
    'api/', 'schema/', 'docs/', 'data/dapper/2026-09-24-v8/',
    'data/dismech-gaps/2026-09-24/',
)
RUNTIME_SCRIPTS = (
    'scripts/lint_scientific_account.py', 'scripts/platform_assets.py',
    'scripts/local_sources.py', 'scripts/local_setup.py', 'scripts/local_deployment.py',
)
FILES = ('.dockerignore', 'services/backend/Dockerfile', 'services/backend/pyproject.toml',
    'data/cfde-genesets/2026-09-24/manifest.json', 'data/cfde-genesets/2026-09-24/activity.json',
    *RUNTIME_SCRIPTS)
REQUIRED = (*FILES, 'services/backend/agent-runtime/dapper-release.json',
    'data/dismech-gaps/2026-09-24/manifest.json', 'data/dismech-gaps/2026-09-24/source-files.json')


def allowed(relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        return False
    if any(part in ('.git', '.runtime', '.venv', '.aws', 'node_modules', '__pycache__', '.pytest_cache')
           or part == '.env' or part.startswith('.env.') for part in path.parts):
        return False
    if path.name.endswith(('.pyc', '.env', '.gpg', '.password.txt', '.key')):
        return False
    if relative.startswith('schema/') and 'generated' in path.parts:
        return False
    return relative in FILES or relative.startswith(DIRECTORIES)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def selected_sources(root, *, include_untracked=False):
    paths = sorted(name.decode() for name in git(root, 'ls-files', '-z').split(b'\0') if name)
    if include_untracked:
        paths = sorted(set(paths) | {name.decode() for name in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if name})
    selected = [name for name in paths if allowed(name)]
    missing = set(REQUIRED) - set(selected)
    if missing:
        raise RuntimeError('Required build sources are not tracked; stage them first: ' + ', '.join(sorted(missing)))
    for relative in selected:
        path = root / relative
        if path.resolve() != path or not path.is_file():
            raise RuntimeError('Build inputs must be regular files without symlinks: ' + relative)
    return selected


def fetch_dockerfile(original):
    """Derive from the source Dockerfile; fail closed when its asset layout changes."""
    replacements = {
        'COPY .deployment-assets/dapper /app/.runtime/dapper': 'COPY --from=assets /assets/dapper /app/.runtime/dapper',
        'COPY .deployment-assets/dismech /app/dismech': 'COPY --from=assets /assets/dismech /app/dismech',
        'COPY .deployment-assets/rds-ca.pem /app/runtime-ca.pem': 'COPY --from=assets /assets/rds-ca.pem /app/runtime-ca.pem',
    }
    for before, after in replacements.items():
        if original.count(before) != 1:
            raise RuntimeError('Backend Dockerfile asset layout changed; review the platform exporter')
        original = original.replace(before, after)
    base = original.splitlines()[0]
    if not base.startswith('FROM ') or not base.endswith(' AS runtime'):
        raise RuntimeError('Backend Dockerfile runtime stage changed; review the platform exporter')
    image = base.removeprefix('FROM ').removesuffix(' AS runtime')
    stage = f'''FROM {image} AS assets
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install PyYAML==6.0.2 python-dotenv==1.1.0
WORKDIR /source
COPY scripts/platform_assets.py scripts/local_sources.py scripts/local_setup.py scripts/local_deployment.py /source/scripts/
COPY services/backend/src/reveal_backend/__init__.py services/backend/src/reveal_backend/dapper_release.py services/backend/src/reveal_backend/evidence_package.py /source/services/backend/src/reveal_backend/
COPY services/backend/agent-runtime/dapper-release.json /source/services/backend/agent-runtime/dapper-release.json
COPY data/dismech-gaps/2026-09-24/manifest.json data/dismech-gaps/2026-09-24/source-files.json /source/data/dismech-gaps/2026-09-24/
RUN python scripts/platform_assets.py --output /assets

'''
    return stage + original


def configured_source(root, env, key, fallback):
    value = Path(env.get(key) or fallback).expanduser()
    return value if value.is_absolute() else root / value


def export(output, *, root=ROOT, allow_dirty=False, fetch_assets=False):
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Refusing to overwrite an existing output directory: ' + str(output))
    dirty = bool(git(root, 'status', '--porcelain', '--untracked-files=all'))
    if dirty and not allow_dirty:
        raise RuntimeError('Commit the source changes first, or use --allow-dirty for a local draft build')
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    selected = selected_sources(root, include_untracked=allow_dirty)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reveal-platform-', dir=output.parent) as temporary:
        staging = Path(temporary)
        for relative in selected:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, target)
        original = (staging / 'services/backend/Dockerfile').read_text()
        (staging / 'Dockerfile').write_text(fetch_dockerfile(original) if fetch_assets else original)
        lock = json.loads((staging / 'services/backend/agent-runtime/dapper-release.json').read_text())
        dismech_pin = json.loads((staging / 'data/dismech-gaps/2026-09-24/manifest.json').read_text())['source_commit']
        assets = {'mode': 'fetch-at-build' if fetch_assets else 'verified-local',
                  'dapper_commit': lock['commit'], 'dismech_commit': dismech_pin, 'rds_ca_sha256': RDS_CA_SHA256}
        if not fetch_assets:
            # Read only source locations; no environment values enter the bundle.
            env = dotenv_values(root / '.env', interpolate=False)
            dapper = root / '.deployment-assets/dapper'
            if not dapper.exists():
                dapper = configured_source(root, env, 'REVEAL_DAPPER_ROOT', '.runtime/dapper')
            dismech = configured_source(root, env, 'REVEAL_DISMECH_SOURCE', '.runtime/dismech')
            ca = root / '.deployment-assets/rds-ca.pem'
            if not ca.exists():
                ca = configured_source(root, env, 'REVEAL_MYSQL_CA_FILE', '.runtime/certs/rds-ca.pem')
            assets['verification'] = prepare_assets(staging / '.deployment-assets', root=staging,
                dapper=dapper, dismech=dismech, ca=ca)
        files = {}
        for path in sorted(staging.rglob('*')):
            if path.is_file():
                content = path.read_bytes()
                files[path.relative_to(staging).as_posix()] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        manifest = {'format': 'reveal.platform-build/1', 'source_commit': commit, 'source_dirty': dirty,
                    'assets': assets, 'files': files, 'total_bytes': sum(item['bytes'] for item in files.values())}
        (staging / 'build-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        # mkdir is the no-overwrite reservation, including a competing export.
        output.mkdir(exist_ok=False)
        try:
            for path in staging.iterdir():
                path.rename(output / path.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New standalone Docker context; usually platform/reveal')
    parser.add_argument('--allow-dirty', action='store_true', help='Include allowlisted uncommitted/untracked sources and mark the bundle as a draft')
    parser.add_argument('--fetch-assets', action='store_true', help='Fetch public pinned assets in Docker (required for a Git-tracked platform service)')
    args = parser.parse_args()
    manifest = export(args.output, allow_dirty=args.allow_dirty, fetch_assets=args.fetch_assets)
    print(json.dumps({'output': str(args.output.absolute()), 'source_commit': manifest['source_commit'],
        'source_dirty': manifest['source_dirty'], 'files': len(manifest['files']), 'bytes': manifest['total_bytes']}, sort_keys=True))


if __name__ == '__main__':
    main()
