#!/usr/bin/env python3
"""Build the Box toolchain snapshot named by REVEAL_BOX_TOOLCHAIN_SNAPSHOT (operator tool; creates one paid Box).

Snapshots belong to one Upstash Box account, so build one per environment key (QA and
production differ). Rebuild whenever CLAUDE_VERSION or a pinned package changes, and at
least monthly for apt, PyPI and npm freshness. The builder runs exactly the job bootstrap's
toolchain recipe and stamp. It never receives credentials, a bundle, a request or a network
policy change, and never runs the agent, box_upload or box_remote. A Box made from the
snapshot still checks the stamp, its executables and the Claude version, and installs the
toolchain cleanly when any of them differs. --dry-run prints the plan without calling Upstash.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.box_adapter import BoxExecutionAdapter, CLAUDE_VERSION, TOOLCHAIN_FORMAT  # noqa: E402

BUILD = '/tmp/toolchain-build.sh'
CHECK = '/tmp/toolchain-check.py'
# /reveal must hold only the toolchain: any job marker or file would make box_upload or the bootstrap fail closed.
EXPECTED = ['claude', 'state', 'toolchain.sha256', 'venv']
PROBE = r'''import glob,hashlib,json,os,subprocess
def entries(path): return sorted(os.listdir(path)) if os.path.isdir(path) else None
skel=set(entries('/etc/skel') or [])
home=entries('/home/reveal-agent')
lock='/reveal/claude/package-lock.json'
print(json.dumps({'reveal':entries('/reveal'),'state':entries('/reveal/state'),
    'modes':[os.stat(p).st_mode&0o7777 for p in ('/reveal','/reveal/state')],
    'owners':[os.stat(p).st_uid for p in ('/reveal','/reveal/state')],
    'temporary':sorted(glob.glob('/tmp/reveal-*')),'credentials':[p for p in ('/reveal/credentials.json','/tmp/reveal-credential.json') if os.path.lexists(p)],
    'home':None if home is None else sorted(set(home)-skel),
    'stamp':open('/reveal/toolchain.sha256').read().strip(),
    'claude':subprocess.run(['/reveal/claude/node_modules/.bin/claude','--version'],capture_output=True,text=True,timeout=120).stdout.strip(),
    'python':subprocess.run(['/reveal/venv/bin/python','-m','pip','freeze','--all'],capture_output=True,text=True,timeout=120).stdout.split(),
    'npm_lock_sha256':hashlib.sha256(open(lock,'rb').read()).hexdigest() if os.path.exists(lock) else None}))
'''


def plan(claude_version=CLAUDE_VERSION):
    stamp = BoxExecutionAdapter.toolchain_stamp(claude_version)
    # The job bootstrap itself (stamp branch included) installs the toolchain; only apt's cache is dropped.
    script = BoxExecutionAdapter.bootstrap_script(claude_version, unpack=False) + 'sudo apt-get clean\n'
    return {'format': TOOLCHAIN_FORMAT, 'claude_version': claude_version, 'stamp': stamp,
            'name': 'reveal-toolchain-' + stamp[:16], 'labels': ['reveal-toolchain', 'stamp-' + stamp[:16]], 'script': script}


def problems(facts, expected):
    """Everything that would make the snapshot unsafe or not the pinned toolchain; empty when clean."""
    found = []
    if facts.get('reveal') != EXPECTED: found.append('/reveal holds more than the toolchain')
    if facts.get('state') != []: found.append('/reveal/state is not empty')
    if facts.get('modes') != [0o755, 0o755] or facts.get('owners') != [0, 0]: found.append('/reveal is not root-owned 0755')
    if facts.get('temporary') or facts.get('credentials'): found.append('job files or credentials are present')
    if facts.get('home') != []: found.append('reveal-agent home is not pristine')
    if facts.get('stamp') != expected['stamp']: found.append('toolchain stamp differs')
    if str(facts.get('claude') or '').split()[:1] != [expected['claude_version']]: found.append('Claude version differs')
    if not facts.get('python') or not facts.get('npm_lock_sha256'): found.append('toolchain manifest is incomplete')
    return found


async def run(box, command):
    result = await box.exec.command(command)
    if str(result.status) != 'completed': raise RuntimeError('Builder command did not complete')
    return result.result


async def build(environ, factory, *, delete_builder=False, emit=print):
    expected = plan()
    box = await factory.create(runtime='node', api_key=environ['UPSTASH_BOX_API_KEY'], labels=expected['labels'])
    snapshot = None
    try:
        emit(json.dumps({'builder_box_id': box.id, 'stamp': expected['stamp']}))
        await box.files.write(path=BUILD, content=expected['script'])
        await run(box, 'sh ' + BUILD + ' && rm -f ' + BUILD)
        await box.files.write(path=CHECK, content=PROBE)
        facts = json.loads(await run(box, 'sudo -n /usr/bin/python3 ' + CHECK + ' && rm -f ' + CHECK))
        found = problems(facts, expected)
        if found: raise RuntimeError('Builder Box is not a clean toolchain: ' + '; '.join(found))
        snapshot = await box.snapshot(name=expected['name'])
        if snapshot.status != 'ready': raise RuntimeError('Snapshot is not ready')
        result = {'snapshot_id': snapshot.id, 'snapshot_name': snapshot.name, 'size_bytes': snapshot.size_bytes,
                  'builder_box_id': box.id, 'builder_deleted': False, 'format': expected['format'],
                  'claude_version': expected['claude_version'], 'stamp': expected['stamp'],
                  'python_packages': facts['python'], 'npm_lock_sha256': facts['npm_lock_sha256'],
                  'next': 'Set REVEAL_BOX_TOOLCHAIN_SNAPSHOT=' + snapshot.id + ' for the environment that owns this Box API key.'}
        if delete_builder:
            await box.delete(); result['builder_deleted'] = True
        return result
    finally:
        # A failed build leaves nothing worth keeping; a built snapshot keeps its builder unless asked.
        if snapshot is None:
            try: await box.delete()
            except Exception: pass
        await box.aclose()


def main(argv=None, factory=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', type=Path, help='dotenv file holding UPSTASH_BOX_API_KEY (default: the environment)')
    parser.add_argument('--dry-run', action='store_true', help='print the stamp, name and build script; call nothing')
    parser.add_argument('--delete-builder', action='store_true',
                        help='delete the builder Box once the snapshot is ready (first confirm this keeps the snapshot)')
    parser.add_argument('--manifest', type=Path, help='also write the result JSON here')
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(plan(), indent=2)); return 0
    if args.env_file:
        from dotenv import dotenv_values
        environ = dotenv_values(args.env_file)
    else:
        environ = os.environ
    if not environ.get('UPSTASH_BOX_API_KEY'):
        print('UPSTASH_BOX_API_KEY is required', file=sys.stderr); return 2
    if factory is None:
        from upstash_box import AsyncBox
        factory = AsyncBox
    try:
        result = asyncio.run(build(environ, factory, delete_builder=args.delete_builder))
    except Exception as exc:
        # Provider bodies can echo request details; report only the failure class and our own messages.
        print('Toolchain snapshot build failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__),
              file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2)
    if args.manifest: args.manifest.write_text(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
