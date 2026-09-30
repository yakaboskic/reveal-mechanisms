#!/usr/bin/env python3
"""Issue an owner-scoped key into a private file; never deploy configuration.

Use an existing owner-only (0700) output directory. Both outputs must be new.
The handoff is reserved before HTTP and records a pending request_id. If a
response is lost, retain that file and resolve the same idempotent request with
an operator; do not provision again with a new output. This CLI never retries
creation or overwrites/resumes a handoff. Rotation uses --owner-user-id and
verifies that existing active principal; it does not extend its expiry.
"""
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

from local_deployment import read_env


class IssuanceError(Exception):
    """Only fixed, credential-free messages may be exposed to the operator."""


class HttpFailure(IssuanceError):
    def __init__(self, status):
        self.status = status
        super().__init__('API request was refused; no configuration was changed.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_base(value, env):
    if not isinstance(value, str) or re.search(r'[\s\\%]', value):
        raise IssuanceError('Invalid API URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise IssuanceError('Invalid API URL.')
    # Accessing port validates malformed/out-of-range ports without any request.
    parsed.port
    base = value.rstrip('/')
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == 'localhost'
    local = (loopback and env.get('REVEAL_ENVIRONMENT') in ('development', 'test')
             and env.get('SERVICE_ENV') not in ('qa', 'prod'))
    if parsed.scheme == 'http' and not local:
        raise IssuanceError('HTTP is allowed only for explicit development/test loopback URLs.')
    callback = env.get('REVEAL_WORKFLOW_URL', '')
    suffix = '/internal/workflows/research-v1'
    if not local and (not callback.endswith(suffix) or callback[:-len(suffix)] != base):
        raise IssuanceError('API URL must match the environment workflow callback base.')
    return base


def private_output(path):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in path.parents):
        raise IssuanceError('Output directories must not be symlinks.')
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise IssuanceError('Output parent must be an existing owner-only directory (0700).')
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'w+', encoding='utf-8')
    finally:
        os.close(parent)


def save(stream, value):
    stream.seek(0)
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write('\n')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def request_json(url, authorization, *, request_id=None):
    headers = {'Authorization': authorization, 'Accept': 'application/json'}
    data = None
    if request_id:
        headers.update({'Idempotency-Key': request_id, 'Content-Type': 'application/json'})
        data = b'{}'
    request = urllib.request.Request(url, headers=headers, data=data,
                                     method='POST' if data else 'GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != (201 if data else 200):
                raise HttpFailure(response.status)
            raw = response.read(65537)
            if len(raw) > 65536:
                raise IssuanceError('API response is invalid.')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise IssuanceError('API response is invalid.')
            return result
    except urllib.error.HTTPError as error:
        raise HttpFailure(error.code) from None


def assertion(env, owner, kind):
    secret = env.get('REVEAL_GATEWAY_SECRET', '')
    if len(secret) < 32:
        raise IssuanceError('Gateway signing configuration is missing.')
    issued = int(time.time())
    claims = {'sub': owner, 'principal_kind': kind, 'iat': issued, 'exp': issued + 60,
              'iss': env.get('REVEAL_GATEWAY_ISSUER', 'reveal-nextjs'),
              'aud': env.get('REVEAL_GATEWAY_AUDIENCE', 'reveal-api'), 'jti': str(uuid4())}
    def encode(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b'=')
    message = b'.'.join(encode(json.dumps(value, separators=(',', ':')).encode())
                        for value in ({'alg': 'HS256', 'typ': 'JWT'}, claims))
    return (message + b'.' + encode(hmac.new(secret.encode(), message, hashlib.sha256).digest())).decode()


def checked_identity(value, *, owner=None, kind=None):
    identity = value.get('user_id')
    if not isinstance(identity, str) or str(UUID(identity)) != identity or (owner and identity != owner):
        raise IssuanceError('API returned an unexpected workspace identity.')
    actual = value.get('principal_kind')
    if actual not in ('anonymous', 'registered') or (kind and actual != kind):
        raise IssuanceError('API returned an unexpected workspace identity.')
    expiry = value.get('workspace_expires_at')
    if actual == 'anonymous':
        if not isinstance(expiry, str):
            raise IssuanceError('Workspace expiry is missing or invalid.')
        parsed = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
            raise IssuanceError('Workspace is expired.')
    elif expiry is not None:
        raise IssuanceError('Registered workspace expiry is invalid.')
    return {'user_id': identity, 'workspace_expires_at': expiry}


def issue(args):
    env = read_env(args.env_file)
    base = api_base(args.api_url, env)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.label):
        raise IssuanceError('Label must contain 1–64 letters, digits, dots, underscores or hyphens.')
    owner = args.owner_user_id
    if owner and str(UUID(owner)) != owner:
        raise IssuanceError('Existing owner must be a canonical UUID.')
    if owner:
        if len(env.get('REVEAL_GATEWAY_SECRET', '')) < 32:
            raise IssuanceError('Gateway signing configuration is missing.')
    elif len(env.get('REVEAL_GATEWAY_SERVICE_TOKEN', '')) < 32:
        raise IssuanceError('Gateway service configuration is missing.')
    with ExitStack() as stack:
        output = stack.enter_context(private_output(args.output))
        config = stack.enter_context(private_output(args.config_output)) if args.config_output else None
        request_id = 'api-key-' + str(uuid4())
        handoff = {'status': 'pending', 'api_url': base, 'label': args.label,
                   'request_id': request_id, 'requested_owner_user_id': owner}
        save(output, handoff)
        if owner:
            for kind in ('anonymous', 'registered'):
                try:
                    result = request_json(base + '/v1/me', 'Bearer ' + assertion(env, owner, kind))
                except HttpFailure as error:
                    if kind == 'anonymous' and error.status == 401:
                        continue
                    raise
                identity = checked_identity(result, owner=owner, kind=kind)
                break
        else:
            result = request_json(base + '/internal/v1/principals/anonymous',
                                  'Bearer ' + env['REVEAL_GATEWAY_SERVICE_TOKEN'], request_id=request_id)
            identity = checked_identity(result, kind='anonymous')
        key = 'rvl_' + secrets.token_urlsafe(32)
        handoff.update(identity, api_key=key, status='issued')
        save(output, handoff)
        if config:
            save(config, {'REVEAL_API_KEY_SHA256': hashlib.sha256(key.encode()).hexdigest(),
                          'REVEAL_API_KEY_USER_ID': identity['user_id']})


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise repeats arbitrary argument values in errors.
        raise IssuanceError('Invalid arguments; use --help for usage.')


def main(argv=None):
    parser = Parser(description=__doc__)
    for flag in ('env-file', 'api-url', 'label', 'output'):
        parser.add_argument('--' + flag, required=True, type=Path if flag in ('env-file', 'output') else str)
    parser.add_argument('--owner-user-id', help='Rotate for an existing active principal; creates no identity.')
    parser.add_argument('--config-output', type=Path, help='New private JSON file containing only hash and owner settings.')
    try:
        issue(parser.parse_args(argv))
    except IssuanceError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Issuance failed; no configuration was installed. Retain any pending handoff before retrying.', file=sys.stderr)
        return 1
    print('Private API key handoff written. Deployment configuration was not changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
