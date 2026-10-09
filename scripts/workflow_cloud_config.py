#!/usr/bin/env python3
"""Prepare private DIG/Vercel configuration; never deploy or print credentials."""
import argparse
import json
import os
import secrets
import stat

from dotenv import dotenv_values
import yaml

from local_deployment import ROOT, env_file, read_env

API_KEY_SETTINGS = ('REVEAL_API_KEY_SHA256', 'REVEAL_API_KEY_USER_ID')
ADMIN_READ_API_KEY_SETTINGS = ('REVEAL_ADMIN_READ_API_KEY_SHA256', 'REVEAL_ADMIN_READ_API_KEY_ID')


def api_key_configuration(runtime, environment):
    """Only the selected environment's private record enables owner API access.

    Shared .env and legacy backend values never cross into QA or production.
    These are a digest and principal UUID; the raw bearer belongs only in the
    separate private delivery file created by the issuance command.
    """
    from reveal_backend.api_keys import configuration
    from reveal_backend.auth import Problem
    path = runtime / f'{environment}-api-key-config.json'
    values = dict.fromkeys(API_KEY_SETTINGS, '')
    if path.is_symlink(): raise ValueError('API key configuration must be a regular private file')
    if path.exists():
        try:
            with path.open('rb') as stream: raw = stream.read(4097)
            if len(raw) > 4096: raise ValueError()
            values = json.loads(raw)
            if (not isinstance(values, dict) or set(values) != set(API_KEY_SETTINGS)
                    or not all(isinstance(value, str) for value in values.values())): raise ValueError()
        except (OSError, ValueError, TypeError):
            raise ValueError('Invalid environment API key configuration record') from None
    try: configuration(values)
    except Problem: raise ValueError('Invalid environment API key configuration pair') from None
    return values


def admin_read_key_configuration(runtime, environment):
    """Only an explicit private environment record enables cross-owner reads."""
    from reveal_backend.admin_read_keys import configuration
    from reveal_backend.auth import Problem
    path = runtime / f'{environment}-admin-read-api-key-config.json'
    values = dict.fromkeys(ADMIN_READ_API_KEY_SETTINGS, '')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return values
    except OSError:
        raise ValueError('Admin read API key configuration must be a regular private file') from None
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError()
            raw = stream.read(4097)
        if len(raw) > 4096: raise ValueError()
        values = json.loads(raw)
        if (not isinstance(values, dict) or set(values) != set(ADMIN_READ_API_KEY_SETTINGS)
                or not all(isinstance(value, str) for value in values.values())):
            raise ValueError()
        configuration(values)
    except (OSError, ValueError, TypeError, Problem):
        raise ValueError('Invalid private admin read API key configuration record') from None
    return values


def prepare(environment):
    if environment not in ('qa', 'prod'):
        raise ValueError('Choose qa or prod')
    runtime = ROOT / '.runtime/workflow'
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = yaml.safe_load((ROOT / 'deploy/dig/service.yaml').read_text())
    settings = {**manifest['env'], **manifest[environment].get('env', {})}
    if 'TYPESAFE_API_KEY' in settings:
        raise ValueError('Typesafe API key configuration belongs only in environment secrets')
    settings['SERVICE_PATH_PREFIX'] = '/api/reveal'
    source = read_env(ROOT / '.runtime/deployment/backend.env')
    source.update({k: v for k, v in dotenv_values(ROOT / '.env', interpolate=False).items() if v is not None})
    source.update(api_key_configuration(runtime, environment))
    admin_keys = admin_read_key_configuration(runtime, environment)
    source.update(admin_keys)
    names = manifest[environment]['secrets']
    admin_names = set(ADMIN_READ_API_KEY_SETTINGS)
    configured_names = admin_names.intersection(names)
    if (configured_names and configured_names != admin_names) or (
            any(value.strip() for value in admin_keys.values()) and configured_names != admin_names):
        raise ValueError('Admin read API key access requires both environment secret references')
    if admin_names.intersection(settings):
        raise ValueError('Admin read API key configuration belongs only in environment secrets')
    keyfile = runtime / f'{environment}-session-keys.json'
    if not keyfile.exists():
        keyfile.write_text(json.dumps({k: secrets.token_hex(32) for k in ('session', 'gateway', 'service')}))
    keyfile.chmod(0o600)
    keys = json.loads(keyfile.read_text())
    source.update(REVEAL_GATEWAY_SECRET=keys['gateway'], REVEAL_GATEWAY_SERVICE_TOKEN=keys['service'])
    source['UPSTASH_VECTOR_WRITE_TOKEN'] = source.get('UPSTASH_VECTOR_WRITE_TOKEN') or source.get('UPSTASH_VECTOR_REST_TOKEN')
    missing = [name for name in names
               if name not in (*API_KEY_SETTINGS, *ADMIN_READ_API_KEY_SETTINGS) and not source.get(name)]
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
    frontend['NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED'] = source.get('NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED', 'false')
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
