#!/usr/bin/env python3
"""Prepare a fresh pinned DAPPER clone and lint tools before launching an agent command."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.dapper_release import prepare_agent_workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True, help='A new directory for this agent start')
    parser.add_argument('--evidence-package', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true', help='Clone and bundle tools without starting Claude Code')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Agent command after --, e.g. claude -p ...')
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not args.prepare_only and not command: parser.error('Supply the agent command after --, or use --prepare-only')
    if args.prepare_only and command: parser.error('--prepare-only cannot also launch a command')
    try:
        runtime = prepare_agent_workspace(args.workspace, ROOT, args.evidence_package,
                                           ROOT / 'services/backend/agent-runtime/dapper-release.json')
        print(json.dumps(runtime, indent=2), flush=True)
        if args.prepare_only: return 0
        environment = dict(os.environ)
        environment.update(REVEAL_DAPPER_ROOT=runtime['dapper_root'], REVEAL_EVIDENCE_PACKAGE=runtime['evidence_package'],
                           PYTHONDONTWRITEBYTECODE='1')
        environment['PATH'] = str(Path(sys.executable).parent) + os.pathsep + environment.get('PATH', '')
        return subprocess.run(command, cwd=runtime['working_directory'], env=environment).returncode
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'Agent startup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
