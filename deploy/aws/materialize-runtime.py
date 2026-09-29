#!/usr/bin/env python3
"""Retrieve the backend secret into root-only /run files using the instance role."""
import json
import os
from pathlib import Path
import re
import subprocess


def materialize(payload, root):
    values, password = payload['backend'], payload['redis_password']
    if not isinstance(values, dict) or not re.fullmatch(r'[a-f0-9]{64}', password):
        raise ValueError('Invalid backend secret structure')
    if any(k.startswith('AWS_') and k not in ('AWS_REGION', 'AWS_S3_US_EAST_1_REGIONAL_ENDPOINT') for k in values):
        raise ValueError('Backend secrets must not contain AWS credential overrides')
    required = {'REVEAL_ARTIFACT_STORE': 's3', 'REVEAL_S3_BUCKET': 'cyaka-reveal-data',
        'REVEAL_S3_PREFIX': 'prod/', 'REVEAL_S3_READ_PREFIXES': 'local/',
        'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal', 'REVEAL_ENVIRONMENT': 'production',
        'REVEAL_LOCAL_DEPLOYMENT': '0', 'REVEAL_JOB_TRANSPORT': 'redis',
        'REVEAL_JOB_NAMESPACE': 'reveal-compose', 'REVEAL_WORK_DIR': '/work',
        'REVEAL_S3_ENDPOINT_URL': '', 'REVEAL_S3_PUBLIC_ENDPOINT_URL': '',
        'REVEAL_REDIS_URL': f'redis://:{password}@redis:6379/0'}
    if any(values.get(k) != v for k, v in required.items()):
        raise ValueError('Backend secret violates the cloud storage/database contract')
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str) or '\n' in value or '\r' in value:
            raise ValueError('Invalid backend environment entry')
    files = {'backend.env': ''.join(f'{k}={v}\n' for k, v in values.items()),
        'redis.env': f'REDISCLI_AUTH={password}\n',
        'redis.conf': 'bind 0.0.0.0\nprotected-mode yes\nport 6379\nsave ""\nappendonly no\n'
                      'maxmemory 256mb\nmaxmemory-policy noeviction\nrequirepass ' + password + '\n'}
    root.mkdir(mode=0o700, parents=True, exist_ok=True); root.chmod(0o700)
    for name, content in files.items():
        temporary = root / (name + '.new')
        with temporary.open('w') as output:
            # Redis drops to its container UID before reading the bind mount.
            # The parent directory remains root-only on the host.
            temporary.chmod(0o444 if name == 'redis.conf' else 0o600)
            output.write(content)
        temporary.replace(root / name)


if __name__ == '__main__':
    os.umask(0o077)
    result = subprocess.run(['aws', 'secretsmanager', 'get-secret-value', '--region', 'us-east-1',
        '--secret-id', 'cyaka/reveal/backend', '--query', 'SecretString', '--output', 'text'],
        check=True, capture_output=True, text=True)
    materialize(json.loads(result.stdout), Path('/run/reveal'))
