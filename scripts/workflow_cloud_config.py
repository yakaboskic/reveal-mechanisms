#!/usr/bin/env python3
"""Prepare private DIG/Vercel configuration; never deploy or print credentials."""
import argparse
import json
import secrets

from dotenv import dotenv_values
import yaml

from local_deployment import ROOT, env_file, read_env


def prepare(environment):
    if environment not in ('qa', 'prod'):
        raise ValueError('Choose qa or prod')
    runtime = ROOT / '.runtime/workflow'
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = yaml.safe_load((ROOT / 'deploy/dig/service.yaml').read_text())
    settings = {**manifest['env'], **manifest[environment].get('env', {})}
    settings['SERVICE_PATH_PREFIX'] = '/api/reveal'
    source = read_env(ROOT / '.runtime/deployment/backend.env')
    source.update({k: v for k, v in dotenv_values(ROOT / '.env', interpolate=False).items() if v is not None})
    keyfile = runtime / f'{environment}-session-keys.json'
    if not keyfile.exists():
        keyfile.write_text(json.dumps({k: secrets.token_hex(32) for k in ('session', 'gateway', 'service')}))
    keyfile.chmod(0o600)
    keys = json.loads(keyfile.read_text())
    source.update(REVEAL_GATEWAY_SECRET=keys['gateway'], REVEAL_GATEWAY_SERVICE_TOKEN=keys['service'])
    source['UPSTASH_VECTOR_WRITE_TOKEN'] = source.get('UPSTASH_VECTOR_WRITE_TOKEN') or source.get('UPSTASH_VECTOR_REST_TOKEN')
    names = manifest[environment]['secrets']
    missing = [name for name in names if not source.get(name)]
    if missing:
        raise ValueError('Missing credentials: ' + ', '.join(missing))
    backend_secret = {name: source[name] for name in names}
    secretfile = runtime / f'{environment}-backend-secret.json'
    # Refuse accidental rotations while preparing an existing release.
    if secretfile.exists() and json.loads(secretfile.read_text()) != backend_secret:
        raise ValueError('Saved deployment credentials differ; reconcile the secret explicitly before preparing')
    secretfile.touch(mode=0o600)
    secretfile.chmod(0o600)
    secretfile.write_text(json.dumps(backend_secret))
    env_file(runtime / f'{environment}-backend.env', {**settings, **backend_secret})
    frontend = {k: v for k, v in source.items() if k.startswith(('AUTH_GOOGLE_', 'AUTH_ORCID_')) or k == 'ADMIN_EMAILS'}
    frontend.update({k: settings[k] for k in ('NEXTAUTH_URL', 'REVEAL_GATEWAY_ISSUER', 'REVEAL_GATEWAY_AUDIENCE')})
    frontend.update(REVEAL_GATEWAY_SECRET=keys['gateway'], REVEAL_GATEWAY_SERVICE_TOKEN=keys['service'],
        AUTH_SECRET=keys['session'], NEXTAUTH_SECRET=keys['session'], DISABLE_ADMIN_LOGIN='false',
        REVEAL_API_URL=f'https://api-{environment}.hugeampkpnbi.org/api/reveal',
        REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS=','.join('https://cyaka-reveal-data.s3.us-east-1.amazonaws.com/'+prefix
            for prefix in ([settings['REVEAL_S3_PREFIX']] + settings.get('REVEAL_S3_READ_PREFIXES', '').split(',')) if prefix))
    env_file(runtime / f'{environment}-frontend.env', frontend)
    return {'environment': environment, 'api': frontend['REVEAL_API_URL'],
        'application_tables': settings['REVEAL_APPLICATION_TABLE_PREFIX'],
        'files': [str(runtime / f'{environment}-{suffix}') for suffix in ('backend.env', 'frontend.env', 'backend-secret.json')]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('environment', choices=('qa', 'prod'))
    print(json.dumps(prepare(parser.parse_args().environment)))
