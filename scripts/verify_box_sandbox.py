#!/usr/bin/env python3
"""Inspect isolation and signal handling in an existing REVEAL Box (no inference)."""
import argparse
import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.box_remote import terminate

async def main(args):
    from dotenv import dotenv_values
    from upstash_box import AsyncBox
    env = dotenv_values(args.env_file) if args.env_file else os.environ
    handle = json.loads(args.handle.read_text())
    box = await AsyncBox.get(handle['box_id'], api_key=env['UPSTASH_BOX_API_KEY'])
    check = '''import os,json,subprocess
print(json.dumps({'uid':os.getuid(),'input_writable':os.access('/reveal/input',os.W_OK),'runtime_writable':os.access('/reveal/workspace/dapper/schema/dapper.yaml',os.W_OK),'ledger_writable':os.access('/reveal/state/ledger',os.W_OK),'output_writable':os.access('/reveal/output',os.W_OK),'sudo_denied':subprocess.run(['sudo','-n','true'],capture_output=True).returncode != 0}))
'''
    try:
        await box.files.write(path='/tmp/reveal-isolation-check.py', content=check)
        run = await box.exec.command('sudo setpriv --reuid 1999 --regid 1999 --clear-groups --no-new-privs python3 /tmp/reveal-isolation-check.py')
        if str(run.status) != 'completed': raise RuntimeError('Box isolation probe failed')
        isolation = json.loads(run.result)
        code = 'import subprocess,pwd,time,json,os\n' + inspect.getsource(terminate) + '''
p=subprocess.Popen(['setpriv','--reuid','1999','--regid','1999','--clear-groups','--no-new-privs','/bin/sleep','60'],start_new_session=True)
time.sleep(0.1)
start=time.monotonic()
terminate(p)
print(json.dumps({'returncode':p.returncode,'elapsed_seconds':time.monotonic()-start}))
'''
        await box.files.write(path='/tmp/reveal-termination-check.py', content=code)
        run = await box.exec.command('sudo python3 /tmp/reveal-termination-check.py')
        if str(run.status) != 'completed': raise RuntimeError('Box termination probe failed')
        termination = json.loads(run.result)
        passed = isolation == {'uid':1999,'input_writable':False,'runtime_writable':False,'ledger_writable':False,'output_writable':True,'sudo_denied':True} and termination['returncode'] == -15
        print(json.dumps({'passed':passed,'isolation':isolation,'termination':termination},indent=2))
        return 0 if passed else 1
    finally:
        await box.aclose()

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--handle',type=Path,required=True)
    p.add_argument('--env-file',type=Path)
    try: raise SystemExit(asyncio.run(main(p.parse_args())))
    except Exception as exc:
        print('Sandbox verification failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(2)
