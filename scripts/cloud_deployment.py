#!/usr/bin/env python3
"""Prepare private cloud configuration without modifying AWS, Vercel, or RDS."""
import argparse
import json
from pathlib import Path
import re
import secrets
from urllib.parse import urlparse

from local_deployment import ROOT, env_file, read_env

RUNTIME = ROOT / '.runtime/cloud'


def https_origin(value):
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port):
        raise ValueError('Use an HTTPS origin without a port, path, or credentials')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', parsed.hostname):
        raise ValueError('Use a public IPv4 address or DNS name')
    return 'https://' + parsed.hostname


def configuration(local_backend, local_frontend, keys, app_url, api_url, image):
    app_url, api_url = https_origin(app_url), https_origin(api_url)
    if not re.fullmatch(r'005901288866\.dkr\.ecr\.us-east-1\.amazonaws\.com/cyaka-reveal-backend@sha256:[a-f0-9]{64}', image):
        raise ValueError('Pin the REVEAL ECR image by its SHA-256 digest')
    # AWS credentials come only from the EC2 instance profile. Never copy the
    # local developer's access keys, profile, endpoint, or metadata overrides.
    backend = {k: v for k, v in local_backend.items()
               if k.startswith(('REVEAL_', 'EMBEDDING_')) or k in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')}
    for key in ('REVEAL_MYSQL_PASSWORD', 'REVEAL_MYSQL_HOST', 'REVEAL_MYSQL_DATABASE'):
        if not backend.get(key): raise ValueError('Missing ' + key)
    backend.update({
        'REVEAL_ENVIRONMENT': 'production', 'REVEAL_LOCAL_DEPLOYMENT': '0',
        'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal', 'REVEAL_JOB_NAMESPACE': 'reveal-compose',
        'REVEAL_ARTIFACT_STORE': 's3', 'REVEAL_S3_BUCKET': 'cyaka-reveal-data',
        'REVEAL_S3_PREFIX': 'prod/', 'REVEAL_S3_READ_PREFIXES': 'local/',
        'REVEAL_S3_ENDPOINT_URL': '', 'REVEAL_S3_PUBLIC_ENDPOINT_URL': '',
        'REVEAL_S3_ADDRESSING_STYLE': 'virtual', 'REVEAL_S3_ENCRYPTION': 'AES256',
        'AWS_REGION': 'us-east-1', 'AWS_S3_US_EAST_1_REGIONAL_ENDPOINT': 'regional',
        'REVEAL_MYSQL_CA_FILE': '/app/runtime-ca.pem', 'REVEAL_WORK_DIR': '/work',
        'REVEAL_JOB_TRANSPORT': 'redis', 'REVEAL_MAX_RUNNING_JOBS': '2',
        'REVEAL_REDIS_URL': f'redis://:{keys["redis"]}@redis:6379/0',
        'REVEAL_GATEWAY_SECRET': keys['gateway'], 'REVEAL_GATEWAY_SERVICE_TOKEN': keys['service'],
        'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api',
        # A local owner's key must not silently acquire production access.
        'REVEAL_API_KEY_SHA256': '', 'REVEAL_API_KEY_USER_ID': '',
        'NEXTAUTH_URL': app_url, 'REVEAL_CANONICAL_URL': app_url, 'REVEAL_PUBLIC_WEB_URL': app_url,
    })
    frontend = {k: v for k, v in local_frontend.items()
                if k.startswith(('AUTH_GOOGLE_', 'AUTH_ORCID_')) or k == 'ADMIN_EMAILS'}
    frontend.update({k: backend[k] for k in ('NEXTAUTH_URL', 'REVEAL_GATEWAY_SECRET',
        'REVEAL_GATEWAY_SERVICE_TOKEN', 'REVEAL_GATEWAY_ISSUER', 'REVEAL_GATEWAY_AUDIENCE')})
    frontend.update({'AUTH_SECRET': keys['session'], 'NEXTAUTH_SECRET': keys['session'],
        'DISABLE_ADMIN_LOGIN': 'false', 'REVEAL_API_URL': api_url,
        'REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS': ','.join(
            'https://cyaka-reveal-data.s3.us-east-1.amazonaws.com/' + prefix for prefix in ('prod/', 'local/'))})
    compose = {'COMPOSE_PROJECT_NAME': 'reveal', 'REVEAL_RUNTIME_DIR': '/run/reveal',
        'REVEAL_BACKEND_IMAGE': image, 'REVEAL_API_HOST': urlparse(api_url).hostname,
        'REVEAL_WORKER_REPLICAS': '2', 'REVEAL_DEPLOY_API_PORT': '18000'}
    return {'backend': backend, 'redis_password': keys['redis']}, frontend, compose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-url', required=True)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True); RUNTIME.chmod(0o700)
    keyfile = RUNTIME / 'keys.json'
    if not keyfile.exists():
        with keyfile.open('x') as output:
            keyfile.chmod(0o600)
            json.dump({key: secrets.token_hex(32) for key in ('session', 'gateway', 'service', 'redis')}, output)
    secret, frontend, compose = configuration(
        read_env(ROOT / '.runtime/deployment/backend.env'),
        read_env(ROOT / '.runtime/deployment/frontend.env'), json.loads(keyfile.read_text()),
        args.app_url, args.api_url, args.image)
    output = RUNTIME / 'backend-secret.json'
    output.touch(mode=0o600); output.chmod(0o600)
    output.write_text(json.dumps(secret))
    env_file(RUNTIME / 'frontend.env', frontend)
    env_file(RUNTIME / 'compose.env', compose)
    print('Prepared private .runtime/cloud files; no remote resources or database records changed.')


if __name__ == '__main__': main()
