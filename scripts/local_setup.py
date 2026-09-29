#!/usr/bin/env python3
"""Prepare a fresh macOS/Linux/WSL2 clone, then start the complete Docker stack."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RDS_CA = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'
DISMECH = 'https://github.com/monarch-initiative/dismech.git'


def run(*command, **kwargs):
    subprocess.run([str(part) for part in command], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, help='Encrypted environment received from Chase; first setup only')
    parser.add_argument('--password-file', type=Path, help='Optional private password file for agent-assisted setup')
    parser.add_argument('--prepare-only', action='store_true', help='Fetch/verify dependencies without starting containers')
    args = parser.parse_args()
    if sys.version_info < (3, 10): raise RuntimeError('Python 3.10+ is required; Python 3.12 is recommended')
    for executable in ('git', 'docker', 'curl', *(['gpg'] if args.bundle else [])):
        if not shutil.which(executable): raise RuntimeError(f'Install {executable}; see README.local.md')
    if args.password_file and not args.bundle: raise RuntimeError('--password-file requires --bundle')
    if args.bundle:
        command = [sys.executable, ROOT / 'scripts/local_handoff.py', 'import', args.bundle.expanduser().resolve()]
        if args.password_file: command.extend(['--password-file', args.password_file.expanduser().resolve()])
        run(*command)
    for path in (ROOT / '.env', ROOT / '.runtime/deployment/storage.env'):
        if not path.is_file(): raise RuntimeError('Missing private configuration; start with --bundle (README.local.md)')
    run('docker', 'info', '--format', 'Docker server: {{.ServerVersion}}')
    result = subprocess.check_output(['docker', 'compose', 'version', '--short'], text=True).strip().lstrip('v')
    if tuple(int(part) for part in result.split('.')[:2]) < (2, 30):
        raise RuntimeError('Docker Compose 2.30+ is required for raw environment files')
    python = ROOT / '.venv/bin/python'
    if not python.exists(): run(sys.executable, '-m', 'venv', ROOT / '.venv')
    print('Installing Python setup/validation dependencies (application runtimes run in Docker).', flush=True)
    run(python, '-m', 'pip', 'install', '-e', str(ROOT / 'services/backend'))
    # Continue in the venv without requiring shell activation.
    run(python, ROOT / 'scripts/local_sources.py')
    if args.prepare_only:
        run(python, ROOT / 'scripts/local_deployment.py', 'prepare')
    else:
        run(python, ROOT / 'scripts/local_deployment.py', 'up', '--build')
        run(python, ROOT / 'scripts/local_deployment.py', 'status')
        print('Ready: http://localhost:3000 — API walkthrough: docs/api-quickstart.md', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    try: main()
    except (RuntimeError, subprocess.CalledProcessError) as error: raise SystemExit(str(error)) from None
