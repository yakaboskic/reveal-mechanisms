#!/usr/bin/env python3
"""Prepare and run an isolated durable local pilot without legacy workers.

Run the pinned local QStash server first (its output stays in the private runtime
directory). Production credentials remain in .env; local scheduler credentials
are read from the development server's log, never printed or sent elsewhere.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from dotenv import dotenv_values
from local_deployment import ROOT, env_file, read_env, storage_config

RUNTIME = ROOT / '.runtime/workflow'


def prepare(*, scheduler='local', api_port=18001, frontend_port=3000, callback_url=None):
    if not 1024 <= api_port <= 65535 or not 1024 <= frontend_port <= 65535 or api_port == frontend_port:
        raise ValueError('Use distinct unprivileged ports')
    source = {k:v for k,v in dotenv_values(ROOT / '.env', interpolate=False).items() if v is not None}
    from reveal_backend.api_keys import configuration as api_key_configuration
    from reveal_backend.auth import Problem
    # Do not inherit an API key bound to the baseline deployment's namespace.
    for key in ('REVEAL_API_KEY_SHA256', 'REVEAL_API_KEY_USER_ID'): source.setdefault(key, '')
    try: api_key_configuration(source)
    except Problem: raise ValueError('Invalid local API key configuration pair') from None
    if callback_url: source['REVEAL_WORKFLOW_URL'] = callback_url
    baseline = ROOT / '.runtime/deployment/backend.env'
    if not baseline.exists():
        raise RuntimeError('Prepare pinned source assets first with scripts/local_deployment.py prepare')
    for path in ('dapper', 'dismech', 'rds-ca.pem'):
        if not (ROOT / '.deployment-assets' / path).exists():
            raise RuntimeError('Missing verified build asset: '+path)
    required = ('UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN',
                'UPSTASH_VECTOR_REST_URL', 'UPSTASH_VECTOR_REST_TOKEN')
    missing = [name for name in required if not source.get(name)]
    if missing: raise RuntimeError('Missing .env settings: '+', '.join(missing))
    RUNTIME.mkdir(parents=True, exist_ok=True); RUNTIME.chmod(0o700)
    saved = RUNTIME / 'local-secrets.json'
    if not saved.exists():
        saved.write_text(json.dumps({k:secrets.token_hex(32) for k in ('session','gateway','service')}))
        saved.chmod(0o600)
    keys = json.loads(saved.read_text())
    backend = read_env(baseline)
    backend.update({k:v for k,v in source.items() if k.startswith(('REVEAL_', 'EMBEDDING_', 'UPSTASH_', 'QSTASH_'))
                    or k in ('ANTHROPIC_API_KEY', 'TYPESAFE_API_KEY')})
    # The supplied project token is used explicitly for the initial migration;
    # deployments can supply a separate ingestion token without changing code.
    backend['UPSTASH_VECTOR_WRITE_TOKEN'] = source.get('UPSTASH_VECTOR_WRITE_TOKEN') or source['UPSTASH_VECTOR_REST_TOKEN']
    for key in list(backend):
        if key.startswith(('REVEAL_REDIS_', 'REVEAL_DISPATCH_')): backend.pop(key)
    if scheduler == 'local':
        log = RUNTIME / 'qstash.log'
        if not log.exists(): raise RuntimeError('Start the local QStash development server; see docs/durable-workflow-runtime.md')
        values = dict(re.findall(r'^(QSTASH_[A-Z_]+)=(\S+)$', log.read_text(), re.M))
        if not all(values.get(k) for k in ('QSTASH_URL','QSTASH_TOKEN','QSTASH_CURRENT_SIGNING_KEY','QSTASH_NEXT_SIGNING_KEY')):
            raise RuntimeError('Local QStash credentials are not ready')
        backend.update(values)
        backend['QSTASH_URL'] = values['QSTASH_URL'].replace('127.0.0.1','host.docker.internal')
        # The scheduler runs on the host; callbacks enter the published API port.
        backend['REVEAL_WORKFLOW_URL'] = f'http://127.0.0.1:{api_port}/internal/workflows/research-v1'
    else:
        required = ('QSTASH_TOKEN','QSTASH_CURRENT_SIGNING_KEY','QSTASH_NEXT_SIGNING_KEY','REVEAL_WORKFLOW_URL')
        missing = [k for k in required if not source.get(k)]
        if missing: raise RuntimeError('Missing managed Workflow settings: '+', '.join(missing))
        if not source['REVEAL_WORKFLOW_URL'].startswith('https://'):
            raise RuntimeError('Managed Workflow requires a reachable HTTPS callback URL')
    storage, download_base = storage_config(ROOT / '.runtime/deployment/storage.env')
    origin = f'http://localhost:{frontend_port}'
    backend.update(storage)
    backend.update(REVEAL_APPLICATION_TABLE_PREFIX='reveal_workflow_local',
        REVEAL_JOB_NAMESPACE='reveal-workflow-local', REVEAL_LOCAL_DEPLOYMENT='1',
        REVEAL_ENVIRONMENT='development', REVEAL_JOB_TRANSPORT='workflow',
        REVEAL_RETRIEVAL_BACKEND='upstash', REVEAL_VECTOR_ENVIRONMENT='local',
        REVEAL_NOTIFICATION_NAMESPACE='reveal-workflow-local',
        REVEAL_MYSQL_CA_FILE='/app/runtime-ca.pem', REVEAL_DAPPER_ROOT='/app/.runtime/dapper',
        REVEAL_DISMECH_SOURCE='/app/dismech', REVEAL_WORK_DIR='/work', TMPDIR='/work', REVEAL_ARTIFACT_STORE='s3',
        REVEAL_MAX_SCRATCH_STEPS='2', REVEAL_WORKSPACE_MAX_BYTES='268435456',
        REVEAL_GATEWAY_SECRET=keys['gateway'], REVEAL_GATEWAY_SERVICE_TOKEN=keys['service'],
        REVEAL_GATEWAY_ISSUER='reveal-nextjs', REVEAL_GATEWAY_AUDIENCE='reveal-api',
        NEXTAUTH_URL=origin, REVEAL_CANONICAL_URL=origin, REVEAL_PUBLIC_WEB_URL=origin)
    backend.pop('SERVICE_PATH_PREFIX', None)
    frontend = {k:v for k,v in source.items() if k.startswith(('AUTH_GOOGLE_','AUTH_ORCID_')) or k == 'ADMIN_EMAILS'}
    frontend.update({k:backend[k] for k in ('REVEAL_GATEWAY_SECRET','REVEAL_GATEWAY_SERVICE_TOKEN',
        'REVEAL_GATEWAY_ISSUER','REVEAL_GATEWAY_AUDIENCE','NEXTAUTH_URL')})
    frontend.update(AUTH_SECRET=keys['session'], NEXTAUTH_SECRET=keys['session'],
        REVEAL_API_URL='http://api:8000', REVEAL_ARTIFACT_DOWNLOAD_BASE_URL=download_base, DISABLE_ADMIN_LOGIN='false')
    env_file(RUNTIME / 'backend.env', backend)
    env_file(RUNTIME / 'frontend.env', frontend)
    env_file(RUNTIME / 'compose.env', {'COMPOSE_PROJECT_NAME':'reveal-workflow-'+hashlib.sha256(str(ROOT).encode()).hexdigest()[:8],
        'REVEAL_RUNTIME_DIR':str(RUNTIME), 'REVEAL_BACKEND_IMAGE':'reveal-workflow-backend:local',
        'REVEAL_FRONTEND_IMAGE':'reveal-workflow-frontend:local', 'REVEAL_DEPLOY_API_PORT':str(api_port),
        'REVEAL_DEPLOY_FRONTEND_PORT':str(frontend_port)})
    return {'frontend':origin, 'api':f'http://127.0.0.1:{api_port}',
        'application_tables':'reveal_workflow_local_*', 'scheduler':scheduler,
        'redis':'managed Pub/Sub; zero recurring reads', 'worker_services':0}


def compose(*args):
    return subprocess.run(['docker','compose','--env-file',str(RUNTIME/'compose.env'),
        '-f',str(ROOT/'deploy/compose.yaml'),'-f',str(ROOT/'deploy/compose.local.yaml'),*args],cwd=ROOT,check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','up','status','logs','down'))
    parser.add_argument('--scheduler', choices=('local','managed'), default='local')
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--api-port',type=int,default=18001)
    parser.add_argument('--frontend-port',type=int,default=3000)
    parser.add_argument('--callback-url',help='Public versioned callback URL when using managed QStash through a local tunnel')
    args=parser.parse_args()
    if args.action in ('prepare','up'):
        config=prepare(scheduler=args.scheduler,api_port=args.api_port,frontend_port=args.frontend_port,callback_url=args.callback_url)
        print(json.dumps(config))
        if args.action=='prepare': return
        compose('config','--quiet')
        if args.build: compose('build','api','frontend')
        compose('run','--rm','--no-deps','-T','bootstrap')
        compose('up','-d','--wait','--wait-timeout','600','api','frontend')
        from configure_workflow_schedule import configure
        print(json.dumps(configure(RUNTIME/'backend.env',apply=True,host=True)))
    elif args.action=='status': compose('ps')
    elif args.action=='logs': compose('logs','--tail','60','api','frontend')
    else: compose('down')


if __name__=='__main__': main()
