#!/usr/bin/env python3
"""Create/update this environment's QStash reconciliation push schedule."""
import argparse
import hashlib
from pathlib import Path
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values
from qstash import QStash


def configure(path, *, apply=False, host=False):
    env={k:v for k,v in dotenv_values(path,interpolate=False).items() if v is not None}
    names=('QSTASH_TOKEN','QSTASH_CURRENT_SIGNING_KEY','QSTASH_NEXT_SIGNING_KEY','REVEAL_WORKFLOW_URL','REVEAL_JOB_NAMESPACE')
    missing=[k for k in names if not env.get(k)]
    if missing: raise ValueError('Missing settings: '+', '.join(missing))
    suffix='/internal/workflows/research-v1'
    url=env['REVEAL_WORKFLOW_URL']
    if not url.endswith(suffix): raise ValueError('Use the canonical versioned workflow URL')
    endpoint=url[:-len(suffix)]+'/internal/workflows/reconcile-v1'
    if urlsplit(endpoint).scheme!='https' and env.get('REVEAL_ENVIRONMENT') not in ('development','test'):
        raise ValueError('A deployed reconciliation endpoint must use HTTPS')
    identity='reveal-reconcile-'+hashlib.sha256((env.get('REVEAL_APPLICATION_TABLE_PREFIX','reveal')+':'+env['REVEAL_JOB_NAMESPACE']).encode()).hexdigest()[:20]
    if apply:
        base=env.get('QSTASH_URL')
        if host and base: base=base.replace('host.docker.internal','127.0.0.1')
        client=QStash(env['QSTASH_TOKEN'],base_url=base,retry=False)
        client.schedule.create(destination=endpoint,cron='* * * * *',body='{}',content_type='application/json',
            method='POST',retries=3,timeout='60s',schedule_id=identity,label=identity)
    return {'schedule_id':identity,'endpoint':endpoint,'interval':'one minute','applied':apply,
        'redis_activity':'PUBLISH only when committed notifications are pending; no Redis reads'}


def main():
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--host',action='store_true',help='Translate local Docker scheduler hostname for a host invocation')
    args=parser.parse_args()
    print(json.dumps(configure(args.env_file,apply=args.apply,host=args.host)))


if __name__=='__main__':main()
