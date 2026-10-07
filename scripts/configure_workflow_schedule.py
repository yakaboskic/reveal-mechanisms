#!/usr/bin/env python3
"""Create/update (or remove) this environment's QStash reconciliation push schedule.

REVEAL_RECONCILE_CRON picks the cadence: one, two or five minutes. A local deployment defaults to five
minutes, everything else to one. Ticks are never retried: a failed or deferred tick is repaired by the next.
"""
import argparse
import hashlib
from pathlib import Path
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values
from qstash import QStash

CADENCES={'* * * * *':'one minute','*/2 * * * *':'two minutes','*/5 * * * *':'five minutes'}


def settings(path):
    env={k:v for k,v in dotenv_values(path,interpolate=False).items() if v is not None}
    names=('QSTASH_TOKEN','QSTASH_CURRENT_SIGNING_KEY','QSTASH_NEXT_SIGNING_KEY','REVEAL_WORKFLOW_URL','REVEAL_JOB_NAMESPACE')
    missing=[k for k in names if not env.get(k)]
    if missing: raise ValueError('Missing settings: '+', '.join(missing))
    identity='reveal-reconcile-'+hashlib.sha256((env.get('REVEAL_APPLICATION_TABLE_PREFIX','reveal')+':'+env['REVEAL_JOB_NAMESPACE']).encode()).hexdigest()[:20]
    return env,identity


def cadence(env):
    cron=env.get('REVEAL_RECONCILE_CRON') or ('*/5 * * * *' if env.get('REVEAL_LOCAL_DEPLOYMENT')=='1' else '* * * * *')
    if cron not in CADENCES: raise ValueError('Unsupported reconcile cadence; use one of: '+', '.join(CADENCES))
    return cron


def scheduler(env, host=False):
    base=env.get('QSTASH_URL')
    if host and base: base=base.replace('host.docker.internal','127.0.0.1')
    return QStash(env['QSTASH_TOKEN'],base_url=base,retry=False)


def configure(path, *, apply=False, host=False):
    env,identity=settings(path)
    suffix='/internal/workflows/research-v1'
    url=env['REVEAL_WORKFLOW_URL']
    if not url.endswith(suffix): raise ValueError('Use the canonical versioned workflow URL')
    endpoint=url[:-len(suffix)]+'/internal/workflows/reconcile-v1'
    if urlsplit(endpoint).scheme!='https' and env.get('REVEAL_ENVIRONMENT') not in ('development','test'):
        raise ValueError('A deployed reconciliation endpoint must use HTTPS')
    cron=cadence(env)
    if apply:
        # retries=0: a busy database answers 200 deferred, and any other failure is repaired by the next tick.
        scheduler(env,host).schedule.create(destination=endpoint,cron=cron,body='{}',content_type='application/json',
            method='POST',retries=0,timeout='60s',schedule_id=identity,label=identity)
    return {'schedule_id':identity,'endpoint':endpoint,'cron':cron,'interval':CADENCES[cron],'applied':apply,
        'redis_activity':'PUBLISH only when committed notifications are pending; no Redis reads'}


def remove(path, *, host=False):
    """Stop the pushes once this stack is down. Never remove it while the stack runs: reconciliation is the
    only repair for dispatch and notification outboxes and stale executions."""
    env,identity=settings(path)
    scheduler(env,host).schedule.delete(identity)
    return {'schedule_id':identity,'removed':True}


def main():
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--remove',action='store_true',help='Delete the schedule (only after this stack is stopped)')
    parser.add_argument('--host',action='store_true',help='Translate local Docker scheduler hostname for a host invocation')
    args=parser.parse_args()
    if args.remove and args.apply: parser.error('--apply and --remove are exclusive')
    print(json.dumps(remove(args.env_file,host=args.host) if args.remove else configure(args.env_file,apply=args.apply,host=args.host)))


if __name__=='__main__':main()
