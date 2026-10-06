#!/usr/bin/env python3
"""Build and operate the Docker stack against the existing dev RDS database."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime/deployment'
ASSETS = ROOT / '.deployment-assets'
FRONTEND_PORT = 3000
FRONTEND_URL = f'http://localhost:{FRONTEND_PORT}'
sys.path.insert(0, str(ROOT / 'services/backend/src'))


def env_file(path, values):
    if any('\n' in str(value) or '\r' in str(value) for value in values.values()):
        raise ValueError('Multiline values are not supported in deployment environment files')
    path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    path.chmod(0o600)


def read_env(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def copy_asset(source, target):
    """Git packfiles are read-only; repeated preparation reuses identical bytes."""
    target = Path(target)
    if target.is_file() and target.read_bytes() == Path(source).read_bytes(): return str(target)
    if target.exists(): target.chmod(target.stat().st_mode | 0o200)
    return shutil.copy2(source, target)


def prepare_dismech(source, index, target):
    """Package frozen documents plus the schema needed to resolve their CURIEs."""
    source, index, target = Path(source).resolve(), Path(index), Path(target)
    manifest = json.loads((index / 'source-files.json').read_text())
    for item in manifest:
        path = (source / item['path']).resolve()
        if not path.is_relative_to(source): raise RuntimeError('DisMech path escapes its source checkout')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']: raise RuntimeError('DisMech source differs from the imported manifest')
        destination = target / item['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.read_bytes() != data: destination.write_bytes(data)
    # The document manifest intentionally contains only KB files. Fetch this
    # additional runtime dependency from the same pinned commit, not mutable HEAD.
    commit = json.loads((index / 'manifest.json').read_text())['source_commit']
    schema = 'src/dismech/schema/dismech.yaml'
    data = subprocess.check_output(['git', '-C', str(source), 'show', f'{commit}:{schema}'])
    destination = target / schema
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def storage_config(path):
    """Real AWS configuration is separate from the legacy development .env."""
    if not path.is_file():
        raise RuntimeError('Copy deploy/storage.env.example to .runtime/deployment/storage.env and select your AWS profile')
    config = read_env(path)
    bucket, prefix, region = (config.get(key, '') for key in ('REVEAL_S3_BUCKET', 'REVEAL_S3_PREFIX', 'AWS_REGION'))
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ValueError('Choose a DNS-compatible S3 bucket without dots for virtual-hosted HTTPS')
    if prefix != 'local/': raise ValueError('The local deployment must use the local/ object prefix')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', region): raise ValueError('Configure AWS_REGION')
    profile = config.get('AWS_PROFILE')
    if config.get('AWS_ACCESS_KEY_ID') or config.get('AWS_SECRET_ACCESS_KEY'):
        if not all(config.get(key) for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')):
            raise ValueError('Shared AWS credentials require both access key and secret key')
        credentials = {'AccessKeyId': config['AWS_ACCESS_KEY_ID'], 'SecretAccessKey': config['AWS_SECRET_ACCESS_KEY'],
                       'SessionToken': config.get('AWS_SESSION_TOKEN'), 'Expiration': config.get('AWS_CREDENTIAL_EXPIRATION')}
    else:
        command = ['aws', *(['--profile', profile] if profile else []), 'configure', 'export-credentials', '--format', 'process']
        credentials = json.loads(subprocess.check_output(command, text=True))
    if credentials.get('Expiration'):
        from datetime import datetime, timezone
        expires = datetime.fromisoformat(credentials['Expiration'].replace('Z', '+00:00'))
        if expires <= datetime.now(timezone.utc):
            raise RuntimeError('AWS credentials expired; refresh storage.env or your AWS profile before startup')
    values = {'REVEAL_S3_BUCKET': bucket, 'REVEAL_S3_PREFIX': prefix, 'AWS_REGION': region,
              'REVEAL_S3_ENDPOINT_URL': '', 'REVEAL_S3_PUBLIC_ENDPOINT_URL': '',
              'REVEAL_S3_ADDRESSING_STYLE': 'virtual', 'REVEAL_S3_ENCRYPTION': 'AES256',
              'AWS_S3_US_EAST_1_REGIONAL_ENDPOINT': 'regional', 'AWS_EC2_METADATA_DISABLED': 'true',
              'AWS_ACCESS_KEY_ID': credentials['AccessKeyId'], 'AWS_SECRET_ACCESS_KEY': credentials['SecretAccessKey']}
    if credentials.get('SessionToken'): values['AWS_SESSION_TOKEN'] = credentials['SessionToken']
    if credentials.get('Expiration'): values['AWS_CREDENTIAL_EXPIRATION'] = credentials['Expiration']
    if credentials.get('Expiration'): print('Local AWS credentials expire at '+credentials['Expiration']+'; refresh with up.', flush=True)
    return values, f'https://{bucket}.s3.{region}.amazonaws.com/{prefix}'


def prepare():
    from dotenv import dotenv_values
    from reveal_backend.dapper_release import verify_release
    env = {key: value for key, value in dotenv_values(ROOT / '.env', interpolate=False).items() if value is not None}
    for key in ('REVEAL_MYSQL_HOST', 'REVEAL_MYSQL_PASSWORD', 'REVEAL_MYSQL_DATABASE', 'REVEAL_MYSQL_CA_FILE'):
        if not env.get(key): raise RuntimeError(f'Configure {key} in the existing root .env')
    storage, download_base = storage_config(RUNTIME / 'storage.env')
    namespace = env.get('REVEAL_JOB_NAMESPACE') or 'reveal-compose'
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', namespace): raise ValueError('Invalid REVEAL_JOB_NAMESPACE')
    RUNTIME.mkdir(parents=True, exist_ok=True); RUNTIME.chmod(0o700)
    ASSETS.mkdir(exist_ok=True)
    saved = RUNTIME / 'local-secrets.json'
    if not saved.exists():
        saved.write_text(json.dumps({key: secrets.token_hex(32) for key in ('session', 'gateway', 'service', 'redis')}))
        saved.chmod(0o600)
    keys = json.loads(saved.read_text())
    dapper = Path(env.get('REVEAL_DAPPER_ROOT') or ROOT / '.runtime/dapper')
    if not dapper.is_absolute(): dapper = ROOT / dapper
    verify_release(dapper, ROOT / 'services/backend/agent-runtime/dapper-release.json')
    shutil.copytree(dapper, ASSETS / 'dapper', dirs_exist_ok=True,
                    copy_function=copy_asset, ignore=shutil.ignore_patterns('__pycache__'))
    verify_release(ASSETS / 'dapper', ROOT / 'services/backend/agent-runtime/dapper-release.json')
    dismech = Path(env.get('REVEAL_DISMECH_SOURCE') or ROOT.parent / 'dismech')
    if not dismech.is_absolute(): dismech = ROOT / dismech
    prepare_dismech(dismech, ROOT / 'data/dismech-gaps/2026-09-24', ASSETS / 'dismech')
    ca = Path(env['REVEAL_MYSQL_CA_FILE']).expanduser()
    if not ca.is_absolute(): ca = ROOT / ca
    shutil.copy2(ca, ASSETS / 'rds-ca.pem')
    backend = {key: value for key, value in env.items()
               if key.startswith(('REVEAL_', 'EMBEDDING_')) or key in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')}
    backend.update({
        'REVEAL_MYSQL_CA_FILE': '/app/runtime-ca.pem', 'REVEAL_DAPPER_ROOT': '/app/.runtime/dapper',
        'REVEAL_DISMECH_SOURCE': '/app/dismech', 'REVEAL_LOCAL_DEPLOYMENT': '1', 'REVEAL_ENVIRONMENT': 'development',
        'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal', 'REVEAL_EXECUTION_MODE': 'box',
        'REVEAL_JOB_TRANSPORT': 'redis', 'REVEAL_JOB_NAMESPACE': namespace,
        'REVEAL_REDIS_URL': f'redis://:{keys["redis"]}@redis:6379/0',
        'REVEAL_MAX_RUNNING_JOBS': '2', 'REVEAL_JOB_LEASE_SECONDS': '90',
        'REVEAL_REDIS_RECLAIM_MS': '90000', 'REVEAL_DISPATCH_INTERVAL_SECONDS': '2',
        'REVEAL_ARTIFACT_STORE': 's3', 'REVEAL_WORK_DIR': '/work',
        **storage, 'REVEAL_GATEWAY_SECRET': keys['gateway'],
        'REVEAL_GATEWAY_SERVICE_TOKEN': keys['service'], 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
        'REVEAL_GATEWAY_AUDIENCE': 'reveal-api', 'NEXTAUTH_URL': FRONTEND_URL,
        'REVEAL_CANONICAL_URL': FRONTEND_URL, 'REVEAL_PUBLIC_WEB_URL': FRONTEND_URL,
    })
    frontend = {key: value for key, value in env.items() if key.startswith(('AUTH_GOOGLE_', 'AUTH_ORCID_')) or key == 'ADMIN_EMAILS'}
    frontend.update({key: backend[key] for key in ('REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN',
        'REVEAL_GATEWAY_ISSUER', 'REVEAL_GATEWAY_AUDIENCE', 'NEXTAUTH_URL')})
    frontend.update({'AUTH_SECRET': keys['session'], 'NEXTAUTH_SECRET': keys['session'],
        'REVEAL_API_URL': 'http://api:8000', 'DISABLE_ADMIN_LOGIN': 'false',
        'REVEAL_ARTIFACT_DOWNLOAD_BASE_URL': download_base})
    env_file(RUNTIME / 'backend.env', backend)
    env_file(RUNTIME / 'frontend.env', frontend)
    env_file(RUNTIME / 'redis.env', {'REDISCLI_AUTH': keys['redis']})
    (RUNTIME / 'redis.conf').write_text('bind 0.0.0.0\nprotected-mode yes\nport 6379\nsave ""\nappendonly no\nmaxmemory 256mb\nmaxmemory-policy noeviction\nrequirepass '+keys['redis']+'\n')
    (RUNTIME / 'redis.conf').chmod(0o600)
    env_file(RUNTIME / 'compose.env', {'COMPOSE_PROJECT_NAME': 'reveal-deploy-'+hashlib.sha256(str(ROOT).encode()).hexdigest()[:8],
        'REVEAL_WORKER_REPLICAS': '2', 'REVEAL_DEPLOY_API_PORT': '18000', 'REVEAL_DEPLOY_FRONTEND_PORT': str(FRONTEND_PORT)})
    print(f'Prepared verified source assets and private runtime configuration. Existing RDS tables: reveal_*; job queue: {namespace}.', flush=True)


def compose(*args, capture=False):
    command = ['docker', 'compose', '--env-file', str(RUNTIME / 'compose.env'), '-f', str(ROOT / 'deploy/compose.legacy.yaml'),
               '-f', str(ROOT / 'deploy/compose.local.yaml'), *args]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout if capture else None


def tool(*args):
    output = compose('run', '--rm', '--no-deps', '-T', 'tools', 'python', '-m', 'reveal_backend.deployment', *args, capture=True)
    return json.loads(output.strip().splitlines()[-1])


def wait_probes(ids, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = tool('verify-probes', *ids)
        if result['complete']: return result
        if any(status in ('failed', 'cancelled') for status in result['statuses']): raise RuntimeError('A verification probe failed')
        time.sleep(3)
    raise RuntimeError('Timed out waiting for deployment probes')


def wait_checkpoint(ids):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = tool('status')
        selected = [row for row in status['active_jobs'] if row['id'] in ids]
        if len(selected) == len(ids) and all(row['status'] == 'running' and row['checkpointed'] for row in selected): return
        time.sleep(2)
    raise RuntimeError('No durable active checkpoint was observed')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def verify_download(owner, identity):
    import jwt
    env = read_env(RUNTIME / 'frontend.env')
    token = jwt.encode({'sub': owner, 'principal_kind': 'anonymous', 'iss': 'reveal-browser', 'aud': 'reveal-anonymous',
        'iat': int(time.time()), 'exp': int(time.time())+300}, env['AUTH_SECRET'], algorithm='HS256')
    request = urllib.request.Request(env['NEXTAUTH_URL'].rstrip('/')+'/api/backend/v1/artifacts/'+identity,
        headers={'Cookie': 'reveal-anonymous='+token})
    try:
        urllib.request.build_opener(NoRedirect).open(request, timeout=30)
        raise RuntimeError('The gateway should return an authorized S3 redirect')
    except urllib.error.HTTPError as response:
        if response.code != 307: raise RuntimeError(f'Gateway returned {response.code}') from None
        location = response.headers['Location']
        if not location.startswith(env['REVEAL_ARTIFACT_DOWNLOAD_BASE_URL']): raise RuntimeError('Unexpected artifact destination')
    with urllib.request.urlopen(location, timeout=30) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != identity: raise RuntimeError('Direct download checksum failed')
    try:
        urllib.request.urlopen(request.full_url, timeout=20)
        raise RuntimeError('A private artifact was accessible anonymously')
    except urllib.error.HTTPError as response:
        if response.code not in (401, 404): raise RuntimeError('Unexpected anonymous artifact response') from None
    try:
        urllib.request.urlopen(location.split('?', 1)[0], timeout=20)
        raise RuntimeError('The S3 artifact was accessible without a signature')
    except urllib.error.HTTPError as response:
        if response.code not in (401, 403): raise RuntimeError('Unexpected unsigned S3 response') from None
    return {'gateway_redirect': True, 'sha256_verified': True, 'anonymous_denied': True,
            'unsigned_s3_denied': True, 'bytes': len(data)}


def verify():
    backend = read_env(RUNTIME / 'backend.env')
    if backend.get('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal') == 'reveal':
        raise RuntimeError('Disruptive infrastructure probes cannot run against the existing application tables')
    if any(job['kind'] != 'deployment_probe' for job in tool('status')['active_jobs']):
        raise RuntimeError('Finish active research jobs before disruptive infrastructure verification')
    tool('resume')
    report = {'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scientific_execution': False,
              'storage': {'provider': 'aws-s3', 'bucket': backend['REVEAL_S3_BUCKET'],
                          'prefix': backend['REVEAL_S3_PREFIX'], 'region': backend['AWS_REGION']}}
    print('Verifying two-worker delivery, RDS fencing and S3 downloads...', flush=True)
    first = tool('seed-probes', '--count', '3', '--delay', '12')
    report['pool'] = wait_probes(first['job_ids'])
    if len(report['pool']['workers']) < 2: raise RuntimeError('Both worker replicas must process work')
    report['download'] = verify_download(first['owner'], report['pool']['sha256'][0])
    print('Verifying broker data loss and redispatch from RDS...', flush=True)
    second = tool('seed-probes', '--count', '3', '--delay', '15')
    compose('restart', 'redis')
    report['redis_restart'] = wait_probes(second['job_ids'])
    print('Verifying worker replacement with empty tmpfs and S3 recovery...', flush=True)
    third = tool('seed-probes', '--count', '2', '--delay', '35')
    wait_checkpoint(third['job_ids'])
    compose('kill', '-s', 'SIGKILL', 'worker')
    compose('up', '-d', '--no-deps', '--force-recreate', 'worker')
    report['worker_replacement'] = wait_probes(third['job_ids'])
    if report['worker_replacement']['recovered_from_s3'] != len(third['job_ids']):
        raise RuntimeError('Each replaced worker must restore its input from S3')
    print('Verifying drain completes active work without cancelling it...', flush=True)
    fourth = tool('seed-probes', '--count', '2', '--delay', '15')
    wait_checkpoint(fourth['job_ids'])
    tool('drain', '--wait', '120')
    report['drain'] = wait_probes(fourth['job_ids'])
    tool('resume')
    print('Verifying retained AWS S3 versions after worker replacement...', flush=True)
    report['retained_s3_versions'] = wait_probes(first['job_ids'])
    report['completed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    (RUNTIME / 'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Verification passed. Report: .runtime/deployment/verification.json', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'up', 'status', 'verify', 'down', 'logs'])
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare': return prepare()
    if args.action == 'up':
        # Reject missing/expired credentials or source assets before draining a
        # healthy pool. Building the replacement image also precedes downtime.
        prepare()
        compose('config', '--quiet')
        if args.build: compose('build', 'api', 'frontend')
        # Hand over port 3000 through the existing checkout-scoped supervisor.
        # It stops the host Next.js process and only this checkout's dev containers.
        subprocess.run([sys.executable, str(ROOT / 'scripts/dev_stack.py'), 'down'], cwd=ROOT, check=True)
        if (RUNTIME / 'compose.env').exists():
            running = compose('ps', '--status', 'running', '--services', capture=True).splitlines()
            if 'worker' in running:
                # Use the running container's configuration: regenerated env
                # files may intentionally reconnect a different table namespace.
                compose('exec', '-T', 'api', 'python', '-m', 'reveal_backend.deployment', 'drain', '--wait', '1200')
        compose('up', '-d', '--wait', 'redis')
        compose('run', '--rm', '--no-deps', '-T', 'bootstrap')
        tool('drain')
        compose('up', '-d', '--wait', '--wait-timeout', '600', 'api', 'dispatcher', 'worker', 'frontend')
        tool('resume')
        print(f'Local deployment: {FRONTEND_URL}', flush=True)
    elif args.action == 'status': compose('ps'); print(json.dumps(tool('status'), indent=2))
    elif args.action == 'verify': verify()
    elif args.action == 'logs': compose('logs', '--tail', '80', 'api', 'worker', 'dispatcher')
    elif args.action == 'down':
        running = compose('ps', '--status', 'running', '--services', capture=True).splitlines()
        if 'worker' in running: tool('drain', '--wait', '1200')
        compose('down')  # Preserve AWS S3 objects and RDS application records.


if __name__ == '__main__': main()
