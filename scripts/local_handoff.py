#!/usr/bin/env python3
"""Export/import a GnuPG-encrypted local environment. Never print credentials."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'reveal.local-environment/1'
PORTABLE = {
    'REVEAL_DAPPER_ROOT': '.runtime/dapper',
    'REVEAL_DISMECH_SOURCE': '.runtime/dismech',
    'REVEAL_MYSQL_CA_FILE': '.runtime/certs/rds-ca.pem',
    'REVEAL_CANONICAL_URL': 'http://localhost:3000',
    'REVEAL_PUBLIC_WEB_URL': 'http://localhost:3000',
    'NEXTAUTH_URL': 'http://localhost:3000',
    'REVEAL_API_URL': 'http://127.0.0.1:18000',
    'REVEAL_ENVIRONMENT': 'development',
    'REVEAL_EXECUTION_MODE': 'box',
    'DISABLE_ADMIN_LOGIN': 'false',
}
EXCLUDED = {'AUTH_SECRET', 'NEXTAUTH_SECRET', 'REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN',
            'TYPESAFE_API_KEY', 'DATABASE_URL', 'REVEAL_JOB_NAMESPACE', 'REVEAL_ARTIFACTS_DIR'}
STORAGE_KEYS = {'AWS_REGION', 'REVEAL_S3_BUCKET', 'REVEAL_S3_PREFIX', 'AWS_ACCESS_KEY_ID',
                'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_CREDENTIAL_EXPIRATION'}


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing configuration and symlink targets.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())


def allowed_environment():
    return {line.split('=', 1)[0] for line in (ROOT / '.env.example').read_text().splitlines()
            if line and not line.startswith('#') and '=' in line} - EXCLUDED


def gpg(data, password, *, decrypt=False):
    # Passphrase travels through stdin, never arguments or the process environment.
    # Plaintext staging exists only in a mode-0700 temporary directory and is removed.
    with tempfile.TemporaryDirectory(prefix='reveal-env-') as temporary:
        directory = Path(temporary)
        source = directory / 'input'
        private_write(source, data)
        command = ['gpg', '--no-options', '--homedir', str(directory), '--batch', '--no-tty',
                   '--pinentry-mode', 'loopback', '--no-symkey-cache', '--passphrase-fd', '0', '--output', '-']
        command += ['--decrypt'] if decrypt else ['--symmetric', '--cipher-algo', 'AES256', '--force-mdc',
                                                '--s2k-mode', '3', '--s2k-count', '65011712']
        result = subprocess.run([*command, str(source)], input=(password + '\n').encode(), capture_output=True)
        if result.returncode:
            raise RuntimeError('GnuPG failed; check the password and encrypted file. No configuration was installed.')
        return result.stdout


def export_bundle(output):
    from dotenv import dotenv_values
    from local_deployment import storage_config
    output = Path(output).resolve()
    password_path = output.with_suffix('.password.txt')
    if output.exists() or password_path.exists():
        raise RuntimeError('Handoff output already exists; choose a new output name')
    output.parent.mkdir(parents=True, exist_ok=True)
    source = dotenv_values(ROOT / '.env', interpolate=False)
    environment = {key: value for key, value in source.items() if key in allowed_environment() and value is not None}
    environment.update(PORTABLE)
    storage, _ = storage_config(ROOT / '.runtime/deployment/storage.env')
    storage = {key: value for key, value in storage.items() if key in STORAGE_KEYS}
    document = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
                'environment': environment, 'storage': storage}
    password = secrets.token_urlsafe(32)
    plaintext = json.dumps(document, sort_keys=True).encode()
    encrypted = gpg(plaintext, password)
    if gpg(encrypted, password, decrypt=True) != plaintext:
        raise RuntimeError('Encrypted handoff round-trip verification failed')
    private_write(password_path, password + '\n')
    private_write(output, encrypted)
    print(f'Encrypted configuration: {output}\nSeparate password file: {password_path}')
    print('Send the encrypted file via Slack; send the password through a different private channel.')


def dotenv_text(values):
    lines = []
    for key, value in sorted(values.items()):
        if not isinstance(value, str) or any(char in value for char in ('\n', '\r', '\0')):
            raise ValueError('Environment values must be single-line strings')
        lines.append(key + "='" + value.replace('\\', '\\\\').replace("'", "\\'") + "'\n")
    return ''.join(lines)


def import_bundle(bundle, password, *, root=ROOT):
    root = Path(root)
    environment_path, storage_path = root / '.env', root / '.runtime/deployment/storage.env'
    if any(path.exists() or path.is_symlink() for path in (environment_path, storage_path)):
        raise RuntimeError('Refusing to overwrite existing .env or storage.env; import into a fresh clone')
    document = json.loads(gpg(Path(bundle).read_bytes(), password, decrypt=True))
    if document.get('format') != FORMAT: raise ValueError('Unsupported handoff format')
    environment, storage = document['environment'], document['storage']
    if not isinstance(environment, dict) or not isinstance(storage, dict): raise ValueError('Invalid environment bundle')
    if set(environment) - allowed_environment() or set(storage) - STORAGE_KEYS:
        raise ValueError('Unexpected configuration keys in handoff')
    if storage.get('REVEAL_S3_PREFIX') != 'local/': raise ValueError('Handoff must use local/ S3 storage')
    environment.update(PORTABLE)
    # Shared scientific records and login identities, independent worker control.
    environment['REVEAL_JOB_NAMESPACE'] = 'reveal-local-' + secrets.token_hex(6)
    env_text = dotenv_text(environment)
    dotenv_text(storage)  # Validate before writing either file; Compose consumes raw values below.
    for name in ('.runtime', '.runtime/deployment'):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True); directory.chmod(0o700)
    private_write(storage_path, ''.join(f'{key}={value}\n' for key, value in sorted(storage.items())))
    private_write(environment_path, env_text)
    print('Installed private configuration with a unique local queue; using the existing reveal_* RDS tables.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    export = commands.add_parser('export')
    export.add_argument('--output', type=Path, default=ROOT / '.runtime/handoff/reveal-local.env.json.gpg')
    restore = commands.add_parser('import')
    restore.add_argument('bundle', type=Path)
    restore.add_argument('--password-file', type=Path, help='Private file; omit to enter password without echo')
    args = parser.parse_args()
    if args.action == 'export': return export_bundle(args.output)
    password = args.password_file.read_text().strip() if args.password_file else getpass.getpass('Handoff password: ')
    if not password: raise ValueError('A password is required')
    import_bundle(args.bundle, password)


if __name__ == '__main__':
    try: main()
    except (RuntimeError, ValueError, FileNotFoundError) as error: raise SystemExit(str(error)) from None
